# Horse Racing News Bot

競馬ニュースを複数サイトから自動取得し、OpenAI でリライトして WordPress へ自動投稿するボットです。

```
RSS / Scrape → 重複排除 → AI リライト → WordPress REST API
```

---

## セットアップ

```bash
# 1. 仮想環境を作成して有効化
python -m venv .venv
source .venv/bin/activate

# 2. 依存パッケージをインストール
pip install -r requirements.txt

# 3. 環境変数を設定
cp .env.example .env
# .env を編集して OPENAI_API_KEY / WP_BASE_URL / WP_USERNAME / WP_APP_PASSWORD を入力

# 4. ニュースソース・カテゴリ設定を確認・編集
nano config/sources.yaml

# 5. 接続確認
python check_setup.py
```

---

## 実行

```bash
# テスト実行（WordPress への投稿なし）
python run.py --dry-run

# 本番実行
python run.py
```

---

## 定期実行（cron）

```bash
crontab -e
```

```
*/30 * * * * cd /home/youruser/horse-racing-news-bot && .venv/bin/python run.py >> logs/cron.log 2>&1
```
