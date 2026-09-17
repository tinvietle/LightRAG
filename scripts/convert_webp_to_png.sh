#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Convert WebP images to PNG using an installed Ubuntu image tool.

Usage:
  ./scripts/convert_webp_to_png.sh FILE.webp
  ./scripts/convert_webp_to_png.sh DIRECTORY
  ./scripts/convert_webp_to_png.sh DIRECTORY --overwrite
  ./scripts/convert_webp_to_png.sh DIRECTORY --delete-original

Directory input is searched recursively. Existing PNG files are skipped unless
--overwrite is supplied. WebP originals are preserved unless --delete-original
is explicitly supplied.
EOF
}

if [[ $# -lt 1 || $# -gt 3 ]]; then
    usage >&2
    exit 2
fi

source_path=$1
overwrite=false
delete_original=false

shift
for option in "$@"; do
    case "${option}" in
        --overwrite)
            overwrite=true
            ;;
        --delete-original)
            delete_original=true
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: ${option}" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ ! -e "${source_path}" ]]; then
    echo "Input does not exist: ${source_path}" >&2
    exit 1
fi

# ImageMagick is common on Ubuntu. dwebp is provided by the `webp` package.
if command -v magick >/dev/null 2>&1; then
    converter=magick
elif command -v convert >/dev/null 2>&1; then
    converter=convert
elif command -v dwebp >/dev/null 2>&1; then
    converter=dwebp
else
    echo "No WebP converter found." >&2
    echo "Install one with: sudo apt install imagemagick" >&2
    echo "Or install Google's tool with: sudo apt install webp" >&2
    exit 1
fi

converted=0
skipped=0
failed=0

convert_one() {
    local input_path=$1
    local output_path="${input_path%.*}.png"

    if [[ -e "${output_path}" && "${overwrite}" != true ]]; then
        echo "SKIP ${output_path} already exists"
        skipped=$((skipped + 1))
        return
    fi

    echo "CONVERT ${input_path} -> ${output_path}"
    if [[ "${converter}" == dwebp ]]; then
        if ! dwebp "${input_path}" -o "${output_path}"; then
            echo "FAIL ${input_path}" >&2
            failed=$((failed + 1))
            return
        fi
    elif ! "${converter}" "${input_path}" "${output_path}"; then
        echo "FAIL ${input_path}" >&2
        failed=$((failed + 1))
        return
    fi

    converted=$((converted + 1))
    if [[ "${delete_original}" == true ]]; then
        rm -- "${input_path}"
        echo "DELETE ${input_path}"
    fi
}

if [[ -f "${source_path}" ]]; then
    if [[ "${source_path,,}" != *.webp ]]; then
        echo "Input file is not a .webp image: ${source_path}" >&2
        exit 1
    fi
    convert_one "${source_path}"
else
    while IFS= read -r -d '' webp_path; do
        convert_one "${webp_path}"
    done < <(find "${source_path}" -type f -iname '*.webp' -print0)
fi

echo "Done. converted=${converted} skipped=${skipped} failed=${failed}"
if ((failed > 0)); then
    exit 1
fi
