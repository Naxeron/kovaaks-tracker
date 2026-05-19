import json

with open("scratch/global_lb.json") as f:
    data = json.load(f)

for item in data.get("data", [])[:5]:
    print(f"Rank {item['rank']}: {item['webappUsername']} - Points: {item['points']}")
