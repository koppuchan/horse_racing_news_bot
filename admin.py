import os
import yaml
from flask import Flask, request, render_template_string, redirect, url_for

app = Flask(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "sources.yaml")

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>除外キーワード管理</title>
    <style>
        body { font-family: sans-serif; max-width: 600px; margin: 40px auto; padding: 20px; background-color: #f4f4f9; }
        .container { background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
        h1 { font-size: 1.5rem; color: #333; }
        ul { list-style-type: none; padding: 0; }
        li { background: #eee; margin: 8px 0; padding: 12px; border-radius: 4px; display: flex; justify-content: space-between; align-items: center; }
        .delete-btn { background-color: #ff4d4d; color: white; border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; }
        .delete-btn:hover { background-color: #ff1a1a; }
        .add-form { margin-top: 20px; display: flex; gap: 10px; }
        input[type="text"] { flex-grow: 1; padding: 10px; border: 1px solid #ccc; border-radius: 4px; }
        button[type="submit"] { background-color: #4CAF50; color: white; border: none; padding: 10px 20px; border-radius: 4px; cursor: pointer; }
        button[type="submit"]:hover { background-color: #45a049; }
    </style>
</head>
<body>
    <div class="container">
        <h1>ニュース除外キーワード管理</h1>
        <p>リストにあるキーワードがタイトルやURLに含まれる記事は、自動的に投稿から除外されます。</p>
        
        <ul>
            {% for keyword in keywords %}
            <li>
                {{ keyword }}
                <form action="{{ url_for('delete_keyword') }}" method="POST" style="margin:0;">
                    <input type="hidden" name="keyword" value="{{ keyword }}">
                    <button type="submit" class="delete-btn">削除</button>
                </form>
            </li>
            {% endfor %}
            {% if not keywords %}
            <p>除外キーワードは登録されていません。</p>
            {% endif %}
        </ul>

        <form action="{{ url_for('add_keyword') }}" method="POST" class="add-form">
            <input type="text" name="new_keyword" placeholder="新しい除外キーワードを入力" required>
            <button type="submit">追加する</button>
        </form>
    </div>
</body>
</html>
"""

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def save_config(config_data):
    # Use allow_unicode to prevent Japanese characters from turning into \uXXXX
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(config_data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

@app.route("/")
def index():
    config = load_config()
    keywords = config.get("blacklist_keywords", [])
    if keywords is None:
        keywords = []
    return render_template_string(HTML_TEMPLATE, keywords=keywords)

@app.route("/add", methods=["POST"])
def add_keyword():
    keyword = request.form.get("new_keyword", "").strip()
    if keyword:
        config = load_config()
        if "blacklist_keywords" not in config or config["blacklist_keywords"] is None:
            config["blacklist_keywords"] = []
        
        if keyword not in config["blacklist_keywords"]:
            config["blacklist_keywords"].append(keyword)
            save_config(config)
            
    return redirect(url_for("index"))

@app.route("/delete", methods=["POST"])
def delete_keyword():
    keyword = request.form.get("keyword", "")
    config = load_config()
    
    if "blacklist_keywords" in config and config["blacklist_keywords"]:
        if keyword in config["blacklist_keywords"]:
            config["blacklist_keywords"].remove(keyword)
            save_config(config)
            
    return redirect(url_for("index"))

if __name__ == "__main__":
    # 0.0.0.0 to allow external access. Port 5000 is default, we'll use 8080 to avoid conflicts.
    print("管理画面を起動しました。ブラウザで http://(VPSのIPアドレス):8080 にアクセスしてください。")
    app.run(host="0.0.0.0", port=8080)
