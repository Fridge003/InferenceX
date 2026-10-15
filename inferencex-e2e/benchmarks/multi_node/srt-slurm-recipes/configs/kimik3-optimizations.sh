#!/usr/bin/env bash
set -eo pipefail

patch_dir="$(dirname "$(readlink -f "$0")")/kimik3-optimizations"
package_root="$(python3 -c 'import importlib.util; from pathlib import Path; print(Path(importlib.util.find_spec("vllm").origin).parent.parent)')"
cd "$package_root"
exec 9> .kimik3-optimizations.lock
flock 9

for patch_name in \
    fc200bec9e-cute-prefill.patch \
    2454c49e76-online-routed-projection.patch \
    kda-online-quant-padding.patch \
    60948-flashinfer-0.7.1.patch; do
    patch_file="$(mktemp)"
    awk '/^diff --git / {keep=($3 ~ /^a\/vllm\//)} keep' "$patch_dir/$patch_name" > "$patch_file"
    if patch --dry-run --reverse --silent --force --fuzz=0 -p1 < "$patch_file" >/dev/null 2>&1; then
        echo "Already applied: $patch_name"
    else
        patch --dry-run --forward --batch --fuzz=0 -p1 < "$patch_file"
        patch --forward --batch --fuzz=0 -p1 < "$patch_file"
        echo "Applied: $patch_name"
    fi
    rm "$patch_file"
done

uv pip install --system --no-deps tokenspeed-mla==2.0.3 flashinfer-python==0.7.1
uv pip install --system --no-deps flashinfer-cubin==0.7.1 --index-url https://flashinfer.ai/whl/
uv pip install --system flashinfer-jit-cache==0.7.1 --index-url https://flashinfer.ai/whl/cu130
python3 - <<'PY'
import importlib.metadata as metadata

for name, expected in (
    ("tokenspeed-mla", "2.0.3"),
    ("flashinfer-python", "0.7.1"),
    ("flashinfer-cubin", "0.7.1"),
    ("flashinfer-jit-cache", "0.7.1"),
):
    actual = metadata.version(name)
    assert actual.split("+")[0] == expected, (name, actual)
    print(f"{name}=={actual}")
PY
