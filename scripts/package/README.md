# Isolated app staging

`stage.py` assembles a fresh legacy-compatible macOS app with Architecture's
packaged engine dependencies. It never invokes `build.sh` or `build-icon.sh`,
discovers the installed app/helper, downloads dependencies, installs, or launches
an app. This is a packaging primitive, **not B26 acceptance or live qualification**.

## Ownership and inputs

This slice owns `stage.py`, `inventory.json`, this guide and the root
`test_staged_package.py`. It uses the public `model_deck_root_guard` entrypoints.
The JSON inventory owns the legacy resource list, plist values, required engine
files, and supported Swift resource bundles. Update the inventory and fixture
tests together when the app's packaged requirements change.

The packager requires macOS, Python 3.11 or newer and default SIGCHLD handling.
Its owned-process supervisor works on the reviewed macOS Python 3.12 environment
without `os.waitid`. The app's explicitly supplied Python interpreter separately
requires version 3.11 or newer.
Supply all of these explicitly:

- The Architecture source root; it is read for compilation and resource copying.
- Existing, disjoint absolute scratch, state and artifact roots. The existing
  protected-root guard rejects installed app/state paths, source checkout output,
  protected ancestors and nonstandard symlink aliases. Outputs inside the checkout
  are permitted only under its existing `work/isolated` or `work/worktrees` policy.
- A fresh simple output name under the artifact root. An existing file, directory,
  or even dangling symlink is refused. No output replacement option exists.
- An already prepared helper snapshot, its expected binary SHA256, and a matching
  `.source-sha256` file. The snapshot must be outside protected live paths and be a
  regular single-link file. Its source hash must match the supplied checkout's
  `OpenRouterCredentialHelper.swift`. A mismatch stops; this tool never rebuilds
  or re-signs the helper. Supply provenance-qualified inputs rather than copying
  from an automatically discovered live app.
- A pre-extracted offline vendor tree with its manifest described below.
- An absolute Python executable for the resulting app. The packager checks its
  version using `-I -B`; it does not bundle an interpreter or discover one.

The optional `--signing-identity` is the only way to request app signing. Omit it
to leave the staged app unsigned, use `-` explicitly for ad hoc signing, or supply
the exact certificate identity. No environment variable or signing-identity file
is consulted. Signing failure stops without automatic ad hoc fallback. The staged
helper signature is verified even when app signing is omitted, and its bytes are
checked before and after app signing. No `--deep` re-signing occurs.

## Offline vendor manifest

Architecture's MCP imports the engine package from `Resources/vendor`. A
tomlkit-only bundle is rejected. Supply `model_deck`, `model_deck_contracts`
(including schemas), `model_deck_root_guard`, and the dependencies declared by
the source's `python/pyproject.toml`. Presently its direct pins are
`jsonschema[format]==4.23.0` and `tomlkit==0.13.3`; the engine version is `0.0.0`.
The input must include the appropriate platform/interpreter builds of all
transitive dependencies. No package installer runs inside the packager.

`vendor-manifest.json` at that tree's root has these fields:

```json
{
  "format_version": 1,
  "provenance": "Describe the wheel sources, hashes and offline extraction procedure",
  "requirements": ["jsonschema[format]==4.23.0", "tomlkit==0.13.3"],
  "distributions": {
    "model-deck": "0.0.0",
    "jsonschema": "4.23.0",
    "tomlkit": "0.13.3"
  },
  "files": {
    "model_deck/__init__.py": "<lowercase SHA256 of the supplied file>"
  }
}
```

The example is schematic: list **every** supplied distribution using normalized
lowercase hyphenated names, and every regular file by POSIX relative path and
SHA256, excluding only the root manifest itself. Include transitive distributions
and their metadata; the abbreviated example is not an installable vendor tree.
Requirements must match the source project. Distribution names/versions must
match all supplied `.dist-info/METADATA`; the engine's `Requires-Dist` must match
the source too. Every `.py` and `.json` file in the source's three engine packages
must have identical vendor bytes, preventing a consistent manifest from hiding
missing or stale engine modules/resources. Required package/schema/MCP files and
all listed bytes are checked both before building and after copying. Symlinks,
hardlinked files, special files
and Python bytecode are refused. The supplied manifest and per-file hashes are
retained in the final output inventory.

These checks record supplier-provided provenance and byte integrity. They do not
authenticate that provenance, resolve dependency markers/extras, prove transitive
dependency completeness, or prove native wheel/interpreter compatibility. Those
need the later source-independent runtime qualification. Do not label fixture
vendor inputs as genuine engine wheels.

## Invocation and artifact layout

An example invocation, after the inputs above have been independently prepared:

```sh
/opt/homebrew/bin/python3 -B scripts/package/stage.py \
  --source-root '/absolute/Model Deck Architecture' \
  --scratch-root /absolute/isolated/scratch \
  --state-root /absolute/isolated/state \
  --artifact-root /absolute/isolated/artifacts \
  --name qualification-1 \
  --helper-snapshot /absolute/inputs/OpenRouterCredentialHelper \
  --helper-sha256 '<expected lowercase binary SHA256>' \
  --helper-source-hash-file /absolute/inputs/OpenRouterCredentialHelper.source-sha256 \
  --vendor-root /absolute/inputs/vendor \
  --python-executable /opt/homebrew/bin/python3
```

The tool builds the `ModelDeck` SwiftPM release product using explicit package,
scratch, cache, configuration and security paths, with automatic resolution
disabled. Commands use argument arrays and an environment without inherited
provider credentials or Python search paths. Each child command gets a new
process group through a small Python guardian. The guardian runs the exact command
argument array, reports its return code through a private pipe, and stays alive
until the packager terminates and reaps its group. The command does not inherit
the status pipe. A deadline bounds the pipe wait, and EOF/malformed status fails.
The guardian reserves the group ID until cleanup, including when a compiler child
outlives its direct parent. The reported command status is distinct from the
guardian's intentional termination. This does not contain a malicious descendant
that deliberately creates a different session/group. Custom SIGCHLD handling
refuses launch. Output is
captured in the owned scratch tree with a 128 KiB returned-output limit.

`<artifact-root>/<name>/` contains `Model Deck.app` and `inventory.json`.
The app contains the compatibility link
`Contents/MacOS/OpenRouterSettings -> ModelDeck`, the byte-preserved helper in
`Contents/Helpers`, and legacy resources plus the supplied vendor tree in
`Contents/Resources`. Existing icons are copied, never regenerated. The engine
package is shipped inside vendor for consumers such as MCP; this slice adds no
new engine daemon launcher or SDK artifact.

SwiftPM currently generates `Bundle.module` accessors that look for their resource
bundles directly under `Bundle.main.bundleURL`. The packager inspects each newly
generated **release** accessor and accepts only that recognized lookup. It copies
the two declared bundles to the app root accordingly. A missing, duplicate or
different accessor is refused rather than silently relying on a source build
directory. This layout still requires real staged signing and resource-lookup
qualification. Do not relocate the bundles to `Contents/Resources` without
changing and qualifying the lookup contract.

Publication uses macOS `renameatx_np(RENAME_EXCL)` into a fresh output name. There
is no overwrite-capable fallback on another platform. Temporary children are
uniquely created; cleanup uses their retained parent directory handles and checks
the child inode before removal. It never removes caller roots or a replacement
child. Collision leaves the competing destination intact. Use private roots
without concurrent writers: this is not an OS sandbox or a comprehensive defense
against hostile same-user root replacement during compilation. Published output
is not made read-only and no durability or power-loss transaction is claimed.

## Checks and qualification limits

From the Architecture root:

```sh
/opt/homebrew/bin/python3 -B -m unittest test_staged_package -v
```

Packaging tests use generated temporary files and injected command/publisher results.
They exercise actual filesystem output, guards, hashes, resource layout refusal,
offline metadata/provenance, command arguments, optional signing failure behavior,
helper preservation, partial cleanup and publication collisions. Timeout handling
uses fake process objects plus three authorized benign temporary Python process
fixtures: fast exit/exec failure, a child outliving its parent, and timeout. These
exercise the supervisor on macOS Python 3.12. No Swift build, signing command,
credential helper, installed runtime or app launches.

Real Swift compilation, command-flag support on the qualification toolchain,
resource lookup with build outputs absent, native dependency loading, actual
signatures/helper identity, source-independent MCP/headless behavior, legacy
launch/token compatibility and all B26 gates remain for the integration owner.
Installed-host/provider/AX behavior remains separately authorized B27 work.
