import os
import json
import re
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
from google import genai
from notion_client import Client as NotionClient

app = Flask(__name__)
CORS(app)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID")


def get_week_monday(target_date):
    """target_dateが属する週の月曜日を返す"""
    return target_date - timedelta(days=target_date.weekday())


def find_weekly_page(notion, target_date):
    """Notionのタスク管理ページから該当週のページを検索する"""
    monday = get_week_monday(target_date)
    title_pattern = f"{monday.year}/{monday.month}/{monday.day}"

    results = notion.search(
        query=f"一週間のTodo : {title_pattern}",
        filter={"property": "object", "value": "page"},
    )

    for page in results.get("results", []):
        title_parts = page.get("properties", {}).get("title", {}).get("title", [])
        if title_parts:
            title_text = title_parts[0].get("plain_text", "")
            if title_pattern in title_text:
                return page["id"]
    return None


def find_task_synced_block(notion, page_id):
    """ページ内の「タスク」見出し直後の同期ブロックを探す"""
    blocks = notion.blocks.children.list(block_id=page_id)

    found_task_heading = False
    for block in blocks["results"]:
        if block["type"] == "heading_2":
            heading_text = ""
            for rt in block["heading_2"].get("rich_text", []):
                heading_text += rt.get("plain_text", "")
            if heading_text.strip() == "タスク":
                found_task_heading = True
                continue

        if found_task_heading and block["type"] == "synced_block":
            return block["id"]

    return None


def extract_tasks_with_gemini(text):
    """Gemini APIでテキストからタスクを抽出する"""
    prompt = f"""以下のテキストは音声入力で話した内容です。
このテキストからタスクをそのまま抽出してください。
勝手に分解・細分化せず、話された通りのタスクを返してください。

以下のJSON形式で出力してください（他の文章は一切不要）:
{{
  "tasks": ["タスク1", "タスク2", "タスク3"]
}}

--- テキスト ---
{text}
"""

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )

    response_text = response.text.strip()
    json_match = re.search(r"\{[\s\S]*\}", response_text)
    if json_match:
        return json.loads(json_match.group())
    return {"tasks": []}


def append_tasks_to_synced_block(notion, synced_block_id, tasks):
    """同期ブロックにタスクをToDoブロックとして追加する"""
    blocks = []
    for task_title in tasks:
        blocks.append({
            "object": "block",
            "type": "to_do",
            "to_do": {
                "rich_text": [{"type": "text", "text": {"content": task_title}}],
                "checked": False,
            },
        })

    if blocks:
        notion.blocks.children.append(
            block_id=synced_block_id,
            children=blocks,
        )


@app.route("/api/process", methods=["POST"])
def process_voice_input():
    """音声入力テキストを処理してNotionに書き込む"""
    data = request.get_json()
    text = data.get("text", "")
    if not text:
        return jsonify({"error": "テキストが空です"}), 400

    target_date = datetime.now()

    try:
        # Geminiでタスク抽出
        result = extract_tasks_with_gemini(text)
        tasks = result.get("tasks", [])
        if not tasks:
            return jsonify({"error": "タスクを抽出できませんでした"}), 400

        # Notionに書き込み
        notion = NotionClient(auth=NOTION_TOKEN)
        page_id = find_weekly_page(notion, target_date)
        if not page_id:
            return jsonify({"error": "該当する週のNotionページが見つかりません"}), 404

        synced_block_id = find_task_synced_block(notion, page_id)
        if not synced_block_id:
            return jsonify({"error": "タスク用の同期ブロックが見つかりません"}), 404

        append_tasks_to_synced_block(notion, synced_block_id, tasks)

        return jsonify({
            "success": True,
            "tasks": tasks,
            "message": f"{len(tasks)}個のタスクを書き込みました",
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"処理中にエラーが発生しました: {str(e)}"}), 500


@app.route("/api/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
