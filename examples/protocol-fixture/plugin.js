#!/usr/bin/env node
// SPDX-License-Identifier: MIT
//
// Engine-independent JSON-lines plugin fixture exercising the frozen
// `contracts/plugin.v1/lifecycle/*` schemas. Standard library only. No
// filesystem access outside the cwd the host extracts us into, no network,
// no imports of the host runtime, no eval / dynamic require.
//
// Wire discipline:
//   - One JSON object per line on stdin/stdout. No batch frames.
//   - Each frame <= 1 MiB (matches the Python codec budget).
//   - The bounded pre-parser rejects non-finite numbers, decoded duplicate
//     object keys, excessive nesting/node counts, and lone UTF-16 surrogates.
//   - JSON-RPC 2.0 envelopes only. We never echo the raw input back in an
//     error response.
//
// Lifecycle state machine:
//   created -> hello_verified -> active -> draining -> inactive -> deactivated
//
// Canonical inventory method names (primary):
//   plugin.v1.hello
//   plugin.v1.activate
//   plugin.v1.heartbeat
//   plugin.v1.invoke
//   plugin.v1.cancel
//   plugin.v1.drain
//   plugin.v1.deactivate
//
// Legacy lifecycle.* aliases are also accepted and documented in README.
//
// Operations contributed:
//   org.example.protocol-fixture.echo     (effect: read)
//   org.example.protocol-fixture.pending  (effect: read, controllable)

'use strict';

const crypto = require('crypto');
const { TextDecoder } = require('util');

const PLUGIN_ID = 'org.example.protocol-fixture';
const PLUGIN_VERSION = '1.0.0';
const FRAME_LIMIT = 1 * 1024 * 1024;
const MAX_JSON_DEPTH = 64;
const MAX_JSON_NODES = 200000;
const MAX_JSON_ARRAY_ITEMS = 4096;
const MAX_JSON_OBJECT_PROPERTIES = 1024;
const PENDING_DEADLINE_MS = 60 * 1000;
const POST_DEACTIVATE_QUIET_MS = 250;
const HEARTBEAT_REQUIRED_ACTIVATION = true;

const STATE_CREATED = 'created';
const STATE_HELLO_VERIFIED = 'hello_verified';
const STATE_ACTIVE = 'active';
const STATE_DRAINING = 'draining';
const STATE_INACTIVE = 'inactive';
const STATE_DEACTIVATED = 'deactivated';

const STATE_ORDER = Object.freeze({
    [STATE_CREATED]: 0,
    [STATE_HELLO_VERIFIED]: 1,
    [STATE_ACTIVE]: 2,
    [STATE_DRAINING]: 3,
    [STATE_INACTIVE]: 4,
    [STATE_DEACTIVATED]: 5,
});

const LEGACY_TO_CANONICAL = Object.freeze({
    'plugin.v1.lifecycle.hello': 'plugin.v1.hello',
    'plugin.v1.lifecycle.activate': 'plugin.v1.activate',
    'plugin.v1.lifecycle.heartbeat': 'plugin.v1.heartbeat',
    'plugin.v1.lifecycle.invoke': 'plugin.v1.invoke',
    'plugin.v1.lifecycle.cancel': 'plugin.v1.cancel',
    'plugin.v1.lifecycle.drain': 'plugin.v1.drain',
    'plugin.v1.lifecycle.deactivate': 'plugin.v1.deactivate',
});

const CANONICAL_METHODS = new Set([
    'plugin.v1.hello',
    'plugin.v1.activate',
    'plugin.v1.heartbeat',
    'plugin.v1.invoke',
    'plugin.v1.cancel',
    'plugin.v1.drain',
    'plugin.v1.deactivate',
]);

const UTF8_DECODER = new TextDecoder('utf-8', { fatal: true });

// ---------------------------------------------------------------------------
// Strict frame codec (no dependency on any host codec).
// ---------------------------------------------------------------------------

class FrameError extends Error {
    constructor(code, detail) {
        super(detail);
        this.name = 'FrameError';
        this.code = code;
    }
}

function isJsonFiniteNumber(n) {
    return typeof n === 'number' && Number.isFinite(n);
}

function exceedsCodePointLimit(value, maximum) {
    let count = 0;
    for (const _character of value) {
        count++;
        if (count > maximum) return true;
    }
    return false;
}

function hasLoneSurrogate(value) {
    if (typeof value !== 'string') return false;
    for (let i = 0; i < value.length; i++) {
        const code = value.charCodeAt(i);
        if (code >= 0xD800 && code <= 0xDBFF) {
            const next = i + 1 < value.length ? value.charCodeAt(i + 1) : 0;
            if (next < 0xDC00 || next > 0xDFFF) return true;
            i++;
        } else if (code >= 0xDC00 && code <= 0xDFFF) {
            return true;
        }
    }
    return false;
}

function checkJsonValue(value, seen) {
    if (value === null) return;
    const t = typeof value;
    if (t === 'string') {
        if (hasLoneSurrogate(value)) {
            throw new FrameError('invalid_utf8', 'frame has a lone UTF-16 surrogate');
        }
        return;
    }
    if (t === 'boolean') return;
    if (t === 'number') {
        if (!Number.isFinite(value)) {
            throw new FrameError('non_finite_number', 'frame has a non-finite number');
        }
        return;
    }
    if (t !== 'object') {
        throw new FrameError('invalid_json', 'frame has an unsupported value');
    }
    if (seen.has(value)) throw new FrameError('invalid_json', 'frame has a cyclic value');
    seen.add(value);
    try {
        if (Array.isArray(value)) {
            if (value.length > MAX_JSON_ARRAY_ITEMS) {
                throw new FrameError('invalid_json', 'frame array exceeds item bound');
            }
            for (let i = 0; i < value.length; i++) {
                checkJsonValue(value[i], seen);
            }
        } else {
            const keys = Object.keys(value);
            if (keys.length > MAX_JSON_OBJECT_PROPERTIES) {
                throw new FrameError('invalid_json', 'frame object exceeds property bound');
            }
            for (const k of keys) {
                if (hasLoneSurrogate(k)) {
                    throw new FrameError('invalid_utf8', 'frame key has a lone UTF-16 surrogate');
                }
                checkJsonValue(value[k], seen);
            }
        }
    } finally {
        seen.delete(value);
    }
}

function ensureUtf8Strict(text) {
    if (typeof text !== 'string' || hasLoneSurrogate(text)) {
        throw new FrameError('invalid_utf8', 'frame has lone UTF-16 surrogates');
    }
}

function decodeUtf8Strict(bytes) {
    try {
        return UTF8_DECODER.decode(bytes);
    } catch (_) {
        throw new FrameError('invalid_utf8', 'frame is not valid UTF-8');
    }
}

function scanJsonDocument(text) {
    let i = 0;
    const n = text.length;
    let nodes = 0;

    function invalid() {
        throw new FrameError('invalid_json', 'frame is not valid JSON');
    }

    function whitespace() {
        while (i < n && (text[i] === ' ' || text[i] === '\t'
            || text[i] === '\r' || text[i] === '\n')) {
            i++;
        }
    }

    function stringToken() {
        if (text[i] !== '"') invalid();
        const start = i;
        i++;
        while (i < n) {
            const code = text.charCodeAt(i);
            if (code === 0x22) {
                i++;
                let decoded;
                try {
                    decoded = JSON.parse(text.slice(start, i));
                } catch (_) {
                    invalid();
                }
                if (hasLoneSurrogate(decoded)) {
                    throw new FrameError('invalid_utf8', 'frame has a lone UTF-16 surrogate');
                }
                return decoded;
            }
            if (code < 0x20) invalid();
            if (code === 0x5c) {
                i++;
                if (i >= n || !'"\\/bfnrtu'.includes(text[i])) invalid();
                if (text[i] === 'u') {
                    if (i + 4 >= n || !/^[0-9a-fA-F]{4}$/.test(text.slice(i + 1, i + 5))) invalid();
                    i += 5;
                    continue;
                }
            }
            i++;
        }
        invalid();
    }

    function value(depth) {
        if (depth > MAX_JSON_DEPTH) invalid();
        nodes++;
        if (nodes > MAX_JSON_NODES) invalid();
        whitespace();
        if (i >= n) invalid();
        if (text[i] === '{') return objectValue(depth);
        if (text[i] === '[') return arrayValue(depth);
        if (text[i] === '"') { stringToken(); return; }
        for (const literal of ['true', 'false', 'null']) {
            if (text.startsWith(literal, i)) {
                i += literal.length;
                return;
            }
        }
        const match = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/.exec(text.slice(i));
        if (!match) invalid();
        const number = Number(match[0]);
        if (!Number.isFinite(number)) {
            throw new FrameError('non_finite_number', 'frame has a non-finite number');
        }
        i += match[0].length;
    }

    function objectValue(depth) {
        i++;
        whitespace();
        const keys = new Set();
        let properties = 0;
        if (text[i] === '}') { i++; return; }
        while (true) {
            whitespace();
            const key = stringToken();
            if (keys.has(key)) {
                throw new FrameError('duplicate_key', 'frame has a duplicate object key');
            }
            keys.add(key);
            properties++;
            if (properties > MAX_JSON_OBJECT_PROPERTIES) invalid();
            whitespace();
            if (text[i] !== ':') invalid();
            i++;
            value(depth + 1);
            whitespace();
            if (text[i] === '}') { i++; return; }
            if (text[i] !== ',') invalid();
            i++;
        }
    }

    function arrayValue(depth) {
        i++;
        whitespace();
        let items = 0;
        if (text[i] === ']') { i++; return; }
        while (true) {
            value(depth + 1);
            items++;
            if (items > MAX_JSON_ARRAY_ITEMS) invalid();
            whitespace();
            if (text[i] === ']') { i++; return; }
            if (text[i] !== ',') invalid();
            i++;
        }
    }

    whitespace();
    value(0);
    whitespace();
    if (i !== n) invalid();
}

function parseFrameObject(text) {
    ensureUtf8Strict(text);
    scanJsonDocument(text);
    let value;
    try {
        value = JSON.parse(text, function (_key, val) {
            if (typeof val === 'number' && !Number.isFinite(val)) {
                throw new FrameError('non_finite_number', 'frame has a non-finite number');
            }
            return val;
        });
    } catch (err) {
        if (err instanceof FrameError) throw err;
        throw new FrameError('invalid_json', 'frame is not valid JSON');
    }
    if (Array.isArray(value)) {
        throw new FrameError('batch_not_supported', 'batch frames are not supported');
    }
    if (value === null || typeof value !== 'object') {
        throw new FrameError('not_object', 'frame must be a JSON object');
    }
    checkJsonValue(value, new Set());
    return value;
}

// ---------------------------------------------------------------------------
// JSON-RPC envelope helpers.
// ---------------------------------------------------------------------------

function replacerSorted(key, val) {
    if (val && typeof val === 'object' && !Array.isArray(val)) {
        const sorted = {};
        for (const k of Object.keys(val).sort()) sorted[k] = val[k];
        return sorted;
    }
    return val;
}

function writeFrame(obj) {
    const text = JSON.stringify(obj, replacerSorted);
    const bytes = Buffer.from(text, 'utf8');
    if (bytes.length > FRAME_LIMIT) {
        throw new FrameError('frame_too_large', 'outbound frame exceeds maximum size');
    }
    process.stdout.write(bytes);
    process.stdout.write(Buffer.from([0x0a]));
}

function makeResult(id, result) {
    return { jsonrpc: '2.0', id: id, result: result };
}

function makeError(id, code, message) {
    return { jsonrpc: '2.0', id: id === undefined ? null : id, error: { code: code, message: message } };
}

const ERR_METHOD_NOT_FOUND = -32601;
const ERR_INVALID_PARAMS = -32602;
const ERR_INTERNAL = -32603;
const ERR_INVALID_REQUEST = -32600;

function respondError(id, code, message) {
    try { writeFrame(makeError(id, code, message)); } catch (_) { /* best-effort */ }
}

// ---------------------------------------------------------------------------
// Plugin state.
// ---------------------------------------------------------------------------

const state = {
    name: STATE_CREATED,
    activationId: null,
    pending: new Map(), // job_id -> { timer }
};

function transitionTo(next) {
    const prevOrder = STATE_ORDER[state.name];
    const nextOrder = STATE_ORDER[next];
    if (nextOrder === undefined || nextOrder !== prevOrder + 1) {
        throw new FrameError('invalid_state', 'lifecycle transition rejected');
    }
    state.name = next;
}

function canonicaliseMethod(method) {
    if (typeof method !== 'string') return method;
    if (Object.prototype.hasOwnProperty.call(LEGACY_TO_CANONICAL, method)) {
        return LEGACY_TO_CANONICAL[method];
    }
    return method;
}

function isUuid(value) {
    if (typeof value !== 'string') return false;
    if (value.length !== 36) return false;
    const re = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    return re.test(value);
}

function isReverseDomainId(value) {
    if (typeof value !== 'string') return false;
    if (value.length < 3 || value.length > 256) return false;
    const re = /^[a-z][a-z0-9]*(\.[a-z][a-z0-9_-]*)+$/;
    return re.test(value);
}

function ensureObject(value) {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
        throw new FrameError('invalid_params', 'params must be a JSON object');
    }
}

function ensureExactKeys(value, allowed, required) {
    ensureObject(value);
    const allowedKeys = new Set(allowed);
    for (const key of Object.keys(value)) {
        if (!allowedKeys.has(key)) {
            throw new FrameError('invalid_params', 'params contain an unknown field');
        }
    }
    for (const key of required) {
        if (!Object.prototype.hasOwnProperty.call(value, key)) {
            throw new FrameError('invalid_params', 'params are missing a required field');
        }
    }
}

function clearPendingJobs() {
    for (const [, pending] of state.pending) {
        if (pending.timer) clearTimeout(pending.timer);
    }
    state.pending.clear();
}

// ---------------------------------------------------------------------------
// Lifecycle handlers.
// ---------------------------------------------------------------------------

function handleHello(id, params) {
    ensureExactKeys(params, ['offered_api', 'nonce'], []);
    const api = params.offered_api;
    if (!api || typeof api !== 'object' || Array.isArray(api)) {
        return makeError(id, ERR_INVALID_PARAMS, 'offered_api required');
    }
    ensureExactKeys(api, ['major', 'minor'], []);
    if (!Number.isInteger(api.major) || !Number.isInteger(api.minor)) {
        return makeError(id, ERR_INVALID_PARAMS, 'offered_api major/minor required');
    }
    if (api.major !== 1) {
        return makeError(id, ERR_INVALID_PARAMS, 'offered_api major mismatch');
    }
    const nonce = params.nonce === undefined ? '' : params.nonce;
    if (typeof nonce !== 'string' || exceedsCodePointLimit(nonce, 128)) {
        return makeError(id, ERR_INVALID_PARAMS, 'nonce too long');
    }
    transitionTo(STATE_HELLO_VERIFIED);
    return makeResult(id, {
        plugin_id: PLUGIN_ID,
        plugin_version: PLUGIN_VERSION,
        capabilities: ['fixture.js.stdlib'],
    });
}

function handleActivate(id, params) {
    ensureExactKeys(
        params,
        ['activation_token', 'allowed_broker_methods', 'config_revision'],
        ['activation_token', 'allowed_broker_methods'],
    );
    if (typeof params.activation_token !== 'string' || params.activation_token.length === 0
        || exceedsCodePointLimit(params.activation_token, 512)) {
        return makeError(id, ERR_INVALID_PARAMS, 'activation_token required');
    }
    if (!Array.isArray(params.allowed_broker_methods)) {
        return makeError(id, ERR_INVALID_PARAMS, 'allowed_broker_methods required');
    }
    if (params.allowed_broker_methods.length > 64) {
        return makeError(id, ERR_INVALID_PARAMS, 'too many allowed_broker_methods');
    }
    for (const m of params.allowed_broker_methods) {
        if (typeof m !== 'string' || m.length === 0 || exceedsCodePointLimit(m, 128)) {
            return makeError(id, ERR_INVALID_PARAMS, 'allowed_broker_methods entry invalid');
        }
    }
    if (params.config_revision !== undefined
        && (!Number.isInteger(params.config_revision) || params.config_revision < 0)) {
        return makeError(id, ERR_INVALID_PARAMS, 'config_revision invalid');
    }
    state.activationId = crypto.randomUUID();
    transitionTo(STATE_ACTIVE);
    return makeResult(id, {
        activation_id: state.activationId,
        invocation_handle_prefix: 'fixture:',
    });
}

function handleHeartbeat(id, params) {
    ensureExactKeys(params, ['activation_id'], ['activation_id']);
    if (HEARTBEAT_REQUIRED_ACTIVATION) {
        if (typeof params.activation_id !== 'string' || !isUuid(params.activation_id)) {
            return makeError(id, ERR_INVALID_PARAMS, 'activation_id required');
        }
        if (params.activation_id !== state.activationId) {
            return makeError(id, ERR_INVALID_PARAMS, 'activation_id mismatch');
        }
    }
    return makeResult(id, { alive: true });
}

function handleInvoke(id, params) {
    ensureExactKeys(params, ['operation_id', 'input', 'broker_context'], [
        'operation_id', 'input', 'broker_context',
    ]);
    if (typeof params.operation_id !== 'string' || !isReverseDomainId(params.operation_id)) {
        return makeError(id, ERR_INVALID_PARAMS, 'operation_id invalid');
    }
    const ctx = params.broker_context;
    if (!ctx || typeof ctx !== 'object' || Array.isArray(ctx)) {
        return makeError(id, ERR_INVALID_PARAMS, 'broker_context required');
    }
    ensureExactKeys(ctx, [
        'activation_id', 'plugin_id', 'invocation_handle', 'revocation_generation',
    ], [
        'activation_id', 'plugin_id', 'invocation_handle', 'revocation_generation',
    ]);
    if (ctx.activation_id !== state.activationId) {
        return makeError(id, ERR_INVALID_PARAMS, 'broker_context activation_id mismatch');
    }
    if (typeof ctx.invocation_handle !== 'string' || ctx.invocation_handle.length === 0
        || exceedsCodePointLimit(ctx.invocation_handle, 128)) {
        return makeError(id, ERR_INVALID_PARAMS, 'broker_context invocation_handle invalid');
    }
    if (!Number.isInteger(ctx.revocation_generation) || ctx.revocation_generation < 0) {
        return makeError(id, ERR_INVALID_PARAMS, 'broker_context revocation_generation invalid');
    }
    if (ctx.plugin_id !== PLUGIN_ID) {
        return makeError(id, ERR_INVALID_PARAMS, 'broker_context plugin_id mismatch');
    }
    if (params.input === undefined) {
        return makeError(id, ERR_INVALID_PARAMS, 'input required');
    }

    if (params.operation_id === 'org.example.protocol-fixture.echo') {
        return makeResult(id, { output: { echo: params.input } });
    }

    if (params.operation_id === 'org.example.protocol-fixture.pending') {
        const jobId = crypto.randomUUID();
        const timer = setTimeout(() => {
            state.pending.delete(jobId);
        }, PENDING_DEADLINE_MS);
        state.pending.set(jobId, { timer: timer });
        return makeResult(id, { output: { pending: true }, job_id: jobId });
    }

    return makeError(id, ERR_METHOD_NOT_FOUND, 'unknown operation_id');
}

function handleCancel(id, params) {
    ensureExactKeys(params, ['job_id'], ['job_id']);
    if (typeof params.job_id !== 'string' || !isUuid(params.job_id)) {
        return makeError(id, ERR_INVALID_PARAMS, 'job_id required');
    }
    const pending = state.pending.get(params.job_id);
    if (pending) {
        if (pending.timer) clearTimeout(pending.timer);
        state.pending.delete(params.job_id);
    }
    return makeResult(id, { accepted: true });
}

function handleDrain(id, params) {
    ensureExactKeys(params, ['deadline_ms'], ['deadline_ms']);
    if (!Number.isInteger(params.deadline_ms) || params.deadline_ms < 1 || params.deadline_ms > 60000) {
        return makeError(id, ERR_INVALID_PARAMS, 'deadline_ms out of range');
    }
    transitionTo(STATE_DRAINING);
    clearPendingJobs();
    transitionTo(STATE_INACTIVE);
    return makeResult(id, { drained: true });
}

function handleDeactivate(id, params) {
    ensureExactKeys(params, [], []);
    if (state.name === STATE_ACTIVE) {
        transitionTo(STATE_DRAINING);
    }
    if (state.name === STATE_DRAINING) {
        transitionTo(STATE_INACTIVE);
    }
    transitionTo(STATE_DEACTIVATED);
    clearPendingJobs();
    return makeResult(id, { deactivated: true });
}

// ---------------------------------------------------------------------------
// Dispatch.
// ---------------------------------------------------------------------------

const DISPATCH = {
    'plugin.v1.hello': { handler: handleHello, allowedStates: [STATE_CREATED] },
    'plugin.v1.activate': { handler: handleActivate, allowedStates: [STATE_HELLO_VERIFIED] },
    'plugin.v1.heartbeat': { handler: handleHeartbeat, allowedStates: [STATE_ACTIVE] },
    'plugin.v1.invoke': { handler: handleInvoke, allowedStates: [STATE_ACTIVE] },
    'plugin.v1.cancel': { handler: handleCancel, allowedStates: [STATE_ACTIVE] },
    'plugin.v1.drain': { handler: handleDrain, allowedStates: [STATE_ACTIVE] },
    'plugin.v1.deactivate': {
        handler: handleDeactivate,
        allowedStates: [STATE_ACTIVE, STATE_DRAINING, STATE_INACTIVE],
    },
};

function dispatchFrame(frame) {
    if (!frame || typeof frame !== 'object' || Array.isArray(frame)) {
        return makeError(undefined, ERR_INVALID_REQUEST, 'request must be a JSON object');
    }
    if (frame.jsonrpc !== '2.0') {
        return makeError(frame.id, ERR_INVALID_REQUEST, 'jsonrpc must be 2.0');
    }
    const rawMethod = frame.method;
    if (typeof rawMethod !== 'string' || rawMethod.length === 0) {
        return makeError(frame.id, ERR_INVALID_REQUEST, 'method required');
    }
    const method = canonicaliseMethod(rawMethod);
    if (!CANONICAL_METHODS.has(method)) {
        return makeError(frame.id, ERR_METHOD_NOT_FOUND, 'method not found');
    }
    if (state.name === STATE_DEACTIVATED) {
        return makeError(frame.id, ERR_METHOD_NOT_FOUND, 'method not found after deactivate');
    }
    const entry = DISPATCH[method];
    if (!entry.allowedStates.includes(state.name)) {
        return makeError(frame.id, ERR_INVALID_PARAMS, 'lifecycle order violated');
    }
    try {
        const params = frame.params === undefined ? {} : frame.params;
        return entry.handler(frame.id, params);
    } catch (err) {
        if (err instanceof FrameError) {
            return makeError(frame.id, ERR_INVALID_PARAMS, err.message);
        }
        return makeError(frame.id, ERR_INTERNAL, 'internal error');
    }
}

// ---------------------------------------------------------------------------
// Main read loop on process.stdin.
// ---------------------------------------------------------------------------

function main() {
    let buffer = Buffer.alloc(0);

    process.stdin.on('data', (chunk) => {
        if (!Buffer.isBuffer(chunk)) {
            chunk = Buffer.from(typeof chunk === 'string' ? chunk : Array.from(chunk));
        }
        buffer = Buffer.concat([buffer, chunk]);
        while (true) {
            const newlineIdx = buffer.indexOf(0x0a);
            if (newlineIdx < 0) {
                if (buffer.length > FRAME_LIMIT) {
                    respondError(null, ERR_INVALID_REQUEST, 'frame_too_large');
                    cleanupAndExit(2);
                    return;
                }
                break;
            }
            let lineBuf = buffer.slice(0, newlineIdx);
            buffer = buffer.slice(newlineIdx + 1);
            if (lineBuf.length > 0 && lineBuf[lineBuf.length - 1] === 0x0d) {
                lineBuf = lineBuf.slice(0, -1);
            }
            if (lineBuf.length === 0) {
                respondError(null, ERR_INVALID_REQUEST, 'empty_frame');
                continue;
            }
            if (lineBuf.length > FRAME_LIMIT) {
                respondError(null, ERR_INVALID_REQUEST, 'frame_too_large');
                cleanupAndExit(2);
                return;
            }
            let text;
            try {
                text = decodeUtf8Strict(lineBuf);
            } catch (err) {
                respondError(null, ERR_INVALID_REQUEST,
                    err instanceof FrameError ? err.code : 'invalid_utf8');
                continue;
            }
            let frame;
            try {
                frame = parseFrameObject(text);
            } catch (err) {
                if (err instanceof FrameError) {
                    respondError(null, ERR_INVALID_REQUEST, err.code);
                } else {
                    respondError(null, ERR_INVALID_REQUEST, 'invalid_json');
                }
                continue;
            }
            const response = dispatchFrame(frame);
            if (response !== undefined && response !== null) {
                try {
                    writeFrame(response);
                } catch (_) {
                    cleanupAndExit(2);
                    return;
                }
            }
            if (state.name === STATE_DEACTIVATED) {
                setTimeout(() => cleanupAndExit(0), POST_DEACTIVATE_QUIET_MS);
                try { process.stdin.pause(); } catch (_) { /* ignore */ }
                return;
            }
        }
    });

    process.stdin.on('end', () => {
        cleanupAndExit(0);
    });

    process.stdin.on('error', () => {
        cleanupAndExit(2);
    });

    process.stdin.resume();
}

let exitCalled = false;
function cleanupAndExit(code) {
    if (exitCalled) return;
    exitCalled = true;
    try {
        for (const [, pending] of state.pending) {
            if (pending.timer) clearTimeout(pending.timer);
        }
        state.pending.clear();
    } catch (_) { /* ignore */ }
    try { process.stdout.end(); } catch (_) { /* ignore */ }
    try { process.stdin.pause(); } catch (_) { /* ignore */ }
    process.exit(code);
}

if (require.main === module) {
    main();
}

module.exports = {
    PLUGIN_ID: PLUGIN_ID,
    PLUGIN_VERSION: PLUGIN_VERSION,
    CANONICAL_METHODS: CANONICAL_METHODS,
    LEGACY_TO_CANONICAL: LEGACY_TO_CANONICAL,
    STATE_ORDER: STATE_ORDER,
    parseFrameObject: parseFrameObject,
    handleHello: handleHello,
    handleActivate: handleActivate,
    handleHeartbeat: handleHeartbeat,
    handleInvoke: handleInvoke,
    handleCancel: handleCancel,
    handleDrain: handleDrain,
    handleDeactivate: handleDeactivate,
    dispatchFrame: dispatchFrame,
    ensureUtf8Strict: ensureUtf8Strict,
    decodeUtf8Strict: decodeUtf8Strict,
    isUuid: isUuid,
    isReverseDomainId: isReverseDomainId,
    transitionTo: transitionTo,
    resetStateForTests: function () {
        state.name = STATE_CREATED;
        state.activationId = null;
        for (const [, pending] of state.pending) {
            if (pending.timer) clearTimeout(pending.timer);
        }
        state.pending.clear();
    },
    getState: function () { return state.name; },
};
