from pathlib import Path
import json

def iter_case_files(input_dir: Path) -> list[Path]:
    return sorted(path for path in input_dir.rglob("*.json") if path.is_file())

def read_case_json(json_path: Path) -> dict:
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {json_path}: {exc}") from exc

folder_path = "/home/tin/LightRAG/sft_prompt/fold1"
# Create output dir in father folder of folder_path
output_dir = Path(folder_path).parent / "txt_plain"
output_dir.mkdir(parents=True, exist_ok=True)

json_files = iter_case_files(Path(folder_path))
count = 0
for json_file in json_files:
    case_data = read_case_json(json_file)
    file_name = json_file.stem
    # print(case_data)
    case_text = case_data[0].get("full_text_cutoff", "")
    if not case_text:
        print(f"Warning: No 'full_text_cutoff' found in {json_file}, skipping.")
        continue
    with open(output_dir / f"{file_name}.txt", "w", encoding="utf-8") as f:
        f.write("")
        count += 1
print(f"Total files: {len(json_files)}, successfully written: {count}, skipped: {len(json_files) - count}")
        