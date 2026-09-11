"""Read subscription limits and selected-key spend without running inference."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import selectors
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from codex_runtime import discover_runtime

CODEX = "/Applications/Codex.app/Contents/Resources/codex"
KEY_ENDPOINT = "https://openrouter.ai/api/v1/key"
MAX_OUTPUT = 64 * 1024


def number(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) else None
    except OverflowError:
        return None


def nonnegative_number(value):
    parsed = number(value)
    return parsed if parsed is not None and parsed >= 0 else None


def daily_usage(usage):
    buckets = usage.get("dailyUsageBuckets") if isinstance(usage, dict) else None
    if buckets is None:
        return None
    if not isinstance(buckets, list):
        raise ValueError("Invalid daily usage")
    days = {}
    for bucket in buckets:
        if not isinstance(bucket, dict):
            raise ValueError("Invalid daily usage bucket")
        start = bucket.get("startDate")
        if not isinstance(start, str) or date.fromisoformat(start).isoformat() != start or start in days:
            raise ValueError("Invalid or duplicate daily usage date")
        days[start] = {"date": start, "tokens": nonnegative_number(bucket.get("tokens"))}
    return [days[start] for start in sorted(days)[-14:]]


def parse_openai(limits, usage=None):
    windows = []
    buckets = limits.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        legacy = limits.get("rateLimits")
        buckets = {"Codex": legacy} if isinstance(legacy, dict) else {}
    for bucket_name, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        label = bucket.get("limitName") or bucket.get("limitId") or bucket_name
        label = str(label)[:64]
        for window_name in ("primary", "secondary"):
            window = bucket.get(window_name)
            if isinstance(window, dict) and len(windows) < 16:
                windows.append({"name": f"{label} · {window_name}",
                                "pool_id": str(bucket.get("limitId") or bucket_name)[:64],
                                "pool_name": label, "window_kind": window_name,
                                "used_percent": number(window.get("usedPercent")),
                                "window_minutes": number(window.get("windowDurationMins")),
                                "resets_at": number(window.get("resetsAt"))})
    summary = usage.get("summary") if isinstance(usage, dict) else None
    summary = summary if isinstance(summary, dict) else {}
    tokens = nonnegative_number(summary.get("lifetimeTokens"))
    result = {"ok": bool(windows), "windows": windows, "lifetime_tokens": tokens,
              "summary": {"peak_daily_tokens": nonnegative_number(summary.get("peakDailyTokens")),
                          "current_streak_days": nonnegative_number(summary.get("currentStreakDays")),
                          "longest_streak_days": nonnegative_number(summary.get("longestStreakDays"))},
              "daily_usage": None}
    try:
        result["daily_usage"] = daily_usage(usage)
    except (ValueError, TypeError):
        result["usage_error"] = "Daily token usage was malformed and is unavailable."
    if not windows:
        result["error"] = "Subscription usage limits are unavailable."
    return result


def parse_openrouter(payload):
    fields = {"usage_daily": None, "usage_weekly": None, "usage_monthly": None,
              "usage_total": None, "limit_remaining": None, "limit": None}
    additional = {"limit_reset": None, "byok_usage": None, "include_byok_in_limit": None}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {"ok": False, "error": "OpenRouter returned unavailable usage information.", "limit_known": False, **fields, **additional}
    for field in fields:
        fields[field] = number(data.get("usage" if field == "usage_total" else field))
    limit_known = "limit" in data and (data["limit"] is None or fields["limit"] is not None)
    if data.get("limit_reset") in ("daily", "weekly", "monthly"):
        additional["limit_reset"] = data["limit_reset"]
    additional["byok_usage"] = nonnegative_number(data.get("byok_usage"))
    if type(data.get("include_byok_in_limit")) is bool:
        additional["include_byok_in_limit"] = data["include_byok_in_limit"]
    return {"ok": True, "limit_known": limit_known, **fields, **additional}


def stop_process(process):
    # Even after the main executable exits, terminate children in its private group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()
    for stream in (process.stdin, process.stdout):
        if stream:
            stream.close()


class NativeUsageClient:
    def __init__(self):
        executable = discover_runtime()["executable_path"]
        self.process = subprocess.Popen([executable, "app-server"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        start_new_session=True)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.received = 0
        self.sequence = 0
        self.deadline = time.monotonic() + 20

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def request(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.send({"id": request_id, "method": method, "params": params})
        while time.monotonic() < self.deadline:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                message = json.loads(line)
                if message.get("id") == request_id and "method" not in message:
                    if "error" in message:
                        raise ValueError("Native usage method unavailable")
                    result = message.get("result")
                    return result if isinstance(result, dict) else {}
            if not self.selector.select(max(0, self.deadline - time.monotonic())):
                break
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise ValueError("Native usage process stopped")
            self.received += len(chunk)
            if self.received > MAX_OUTPUT:
                raise ValueError("Native usage output exceeded limit")
            self.buffer += chunk
        raise TimeoutError("Native usage timeout")

    def close(self):
        self.selector.close()
        stop_process(self.process)


def collect_openai():
    client = None
    try:
        client = NativeUsageClient()
        client.request("initialize", {"clientInfo": {"name": "openrouter_settings_usage", "version": "1"},
                                      "capabilities": {"experimentalApi": True}})
        client.send({"method": "initialized", "params": {}})
        limits = client.request("account/rateLimits/read", {})
        try:
            usage = client.request("account/usage/read", {})
        except Exception:
            usage = None
        result = parse_openai(limits, usage)
        if usage is None:
            result["usage_error"] = "Token usage history is unavailable. Subscription limits remain available."
        return result
    except Exception:
        result = parse_openai({})
        result["error"] = "Could not read Codex subscription usage. Check your Codex sign-in."
        return result
    finally:
        if client:
            client.close()


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def read_key(account_id):
    if not isinstance(account_id, str) or str(uuid.UUID(account_id)) != account_id.lower():
        raise ValueError("Invalid key account")
    helper = Path(__file__).resolve().parent.parent / "Helpers/OpenRouterCredentialHelper"
    process = subprocess.Popen([str(helper), "--token", account_id], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    secret = b""
    deadline = time.monotonic() + 4
    try:
        while time.monotonic() < deadline:
            if not selector.select(max(0, deadline - time.monotonic())):
                break
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
                if process.returncode != 0 or not secret.strip():
                    raise ValueError("Saved key unavailable")
                return secret.decode("utf-8").strip()
            secret += chunk
            if len(secret) > 16384:
                raise ValueError("Credential output exceeded limit")
        raise TimeoutError("Credential timeout")
    finally:
        selector.close()
        stop_process(process)


def fetch_openrouter(key):
    request = urllib.request.Request(KEY_ENDPOINT, headers={"Authorization": "Bearer " + key,
                                                           "Accept": "application/json"})
    opener = urllib.request.build_opener(NoRedirects())
    with opener.open(request, timeout=10) as response:
        body = response.read(MAX_OUTPUT + 1)
        if len(body) > MAX_OUTPUT:
            raise ValueError("Usage response exceeded limit")
        return parse_openrouter(json.loads(body))


def bounded_openrouter_fetch(key):
    # Socket timeouts alone do not bound a slow trickle of response bytes.
    completed = queue.Queue(maxsize=1)
    def fetch():
        try:
            completed.put((True, fetch_openrouter(key)))
        except Exception as error:
            completed.put((False, error))
    threading.Thread(target=fetch, daemon=True).start()
    success, result = completed.get(timeout=10)
    if not success:
        raise result
    return result


def collect_openrouter(account_id):
    empty = parse_openrouter(None)
    if not account_id:
        return {**empty, "error": "Select a saved OpenRouter key to view its usage."}
    try:
        key = read_key(account_id)
        return bounded_openrouter_fetch(key)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            message = "OpenRouter rejected the saved key. Check it in Settings."
        elif 300 <= error.code < 400:
            message = "OpenRouter redirected the request; credentials were not forwarded."
        else:
            message = "OpenRouter usage is temporarily unavailable."
        return {**empty, "error": message}
    except Exception:
        return {**empty, "error": "Could not read usage for this saved OpenRouter key. Check the key in Settings."}


def collect_usage(account_id):
    with ThreadPoolExecutor(max_workers=2) as executor:
        openai = executor.submit(collect_openai)
        openrouter = executor.submit(collect_openrouter, account_id)
        return {"openai": openai.result(), "openrouter": openrouter.result(),
                "fetched_at": datetime.now(timezone.utc).isoformat()}


def main():
    try:
        request = json.loads(sys.stdin.buffer.read(65537))
        account_id = request.get("account_id", "") if isinstance(request, dict) else ""
    except Exception:
        account_id = ""
    print(json.dumps(collect_usage(account_id), allow_nan=False))


if __name__ == "__main__":
    main()
