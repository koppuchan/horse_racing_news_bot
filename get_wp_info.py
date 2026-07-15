import os
import requests
from dotenv import load_dotenv

load_dotenv()
url = os.environ["WP_BASE_URL"]
user = os.environ["WP_USERNAME"]
pw = os.environ["WP_APP_PASSWORD"]
auth = (user, pw)
api = f"{url}/wp-json/wp/v2"

categories_to_create = ["追い切り", "レース結果", "ニュース"]

print("--- Categories ---")
resp = requests.get(f"{api}/categories", auth=auth)
existing_cats = {}
if resp.status_code == 200:
    for cat in resp.json():
        existing_cats[cat['name']] = cat['id']

for name in categories_to_create:
    if name not in existing_cats:
        print(f"Creating category {name}...")
        res = requests.post(f"{api}/categories", auth=auth, json={"name": name})
        if res.status_code == 201:
            existing_cats[name] = res.json()['id']
            print(f"Created {name} with ID {existing_cats[name]}")
        else:
            print(f"Failed to create {name}", res.status_code, res.text)
    else:
        print(f"Category {name} already exists with ID {existing_cats[name]}")

print("Current Categories:", existing_cats)
