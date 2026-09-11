import os
import sys
import json
import subprocess

sys.stdout.reconfigure(encoding='utf-8')

QUEUE_FILE = "overnight_queue.json"
COMPLETED_FILE = "completed_vault_channels.json"

def load_json(filepath, default):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Error reading {filepath}: {e}")
    return default

def save_json(filepath, data):
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def main():
    queue = load_json(QUEUE_FILE, [])
    completed_data = load_json(COMPLETED_FILE, {})
    
    # Normalize completed names
    completed_stories = set()
    if isinstance(completed_data, dict):
        for k in completed_data.keys():
            completed_stories.add(k.lower().strip())
    elif isinstance(completed_data, list):
        for item in completed_data:
            if isinstance(item, dict) and 'story' in item:
                completed_stories.add(item['story'].lower().strip())

    print(f"📋 Master Queue Count: {len(queue)} stories")
    print(f"🏆 Completed Stories: {len(completed_stories)}")
    
    # Find next pending story
    next_story = None
    for s in queue:
        story_name = s if isinstance(s, str) else s.get('name', '')
        if story_name.lower().strip() not in completed_stories:
            next_story = story_name
            break
            
    if not next_story:
        print("🎉 ALL STORIES IN OVERNIGHT QUEUE HAVE BEEN SUCCESSFULLY COMPLETED!")
        return

    print(f"\n🚀 NEXT QUEUED STORY TO ARCHIVE: {next_story}")
    
    # Dispatch via GitHub CLI
    pat = os.getenv("GH_PAT") or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    cmd = [
        "gh", "workflow", "run", "archive_story.yml",
        "-f", f"story={next_story}",
        "-f", "from_start=false"
    ]
    
    env = os.environ.copy()
    if pat:
        env["GH_TOKEN"] = pat
        
    print(f"Running command: {' '.join(cmd)}")
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    print(f"Status: {res.returncode}")
    print(f"Stdout: {res.stdout.strip()}")
    if res.stderr.strip():
        print(f"Stderr: {res.stderr.strip()}")

if __name__ == "__main__":
    main()
