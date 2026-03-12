import json, os

with open('/Users/sangram.vuppala/Downloads/Logs-2026-03-12 13_12_53.json') as f:
    data = json.load(f)

base_dir = '/Users/sangram.vuppala/Downloads/sangram_bvid_migration'
os.makedirs(base_dir, exist_ok=True)

seen = {}
for entry in data:
    fields = entry.get('fields', {})
    trace_id = fields.get('trace_id') or fields.get('span_id') or str(entry.get('timestamp', ''))
    if trace_id in seen:
        print(f'Duplicate skipped: {trace_id}')
        continue
    seen[trace_id] = True

    bvid = fields.get('businessVerticalId')
    folder_name = str(bvid) if bvid else 'No BVID'
    out_dir = os.path.join(base_dir, folder_name)
    os.makedirs(out_dir, exist_ok=True)

    output = {
        'timestamp': entry.get('timestamp'),
        'date': entry.get('date'),
        **fields
    }
    filepath = os.path.join(out_dir, f'{trace_id}.json')
    with open(filepath, 'w') as f:
        json.dump(output, f, indent=2)
    print(f'Written: {folder_name}/{trace_id}.json  ({fields.get("time", entry.get("date", ""))})')

print(f'\nDone. {len(seen)} unique files written across folders in {base_dir}')
