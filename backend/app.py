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

DAY_MAP = {
    0: "Mon",
    1: "Tue",
    2: "Wed",
    3: "Thu",
    4: "Fri",
    5: "Weekend",
    6: "Weekend",
}

DAY_NAMES_JA = {
    "月曜": 0, "月曜日": 0, "月": 0,
    "火曜": 1, "火曜日": 1, "火": 1,
    "水曜": 2, "水曜日": 2, "水": 2,
    "木曜": 3, "木曜日": 3, "木": 3,
    "金曜": 4, "金曜日": 4, "金": 4,
    "土曜": 5, "土曜日": 5, "土": 5,
    "日曜": 6, "日曜日": 6, "日": 6,
}


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


def decompose_tasks_with_gemini(text, target_date):
    """Gemini APIでテキストからタスクを分解する"""
    today_weekday = target_date.weekday()
    today_name = ["月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日"][today_weekday]

    prompt = f"""あなたはタスク整理のプロです。以下のテキストは、朝に音声入力で話した内容を文字起こししたものです。
今日は{target_date.strftime('%Y/%m/%d')}（{today_name}）です。

このテキストからタスクを抽出し、以下のルールで整理してください：

1. タスクを具体的なアクションに分解する
2. 各タスクにはサブタスク（1〜3個程度）を付ける。サブタスクは実際に手を動かすレベルまで細分化する
3. 「今日」「明日」「水曜日に」などの日付表現がある場合、該当する曜日に振り分ける
4. 日付指定がないタスクは「今日」（{today_name}）に入れる
5. 曜日は Mon, Tue, Wed, Thu, Fri, Weekend のいずれかで返す

以下のJSON形式で出力してください（他の文章は一切不要）:
{{
  "tasks": [
    {{
      "day": "Mon",
      "title": "タスク名",
      "subtasks": ["サブタスク1", "サブタスク2"]
    }}
  ]
}}

--- テキスト ---
{text}
"""

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt,
    )

    response_text = response.text.strip()
    json_match = re.search(r"\{[\s\S]*\}", response_text)
    if json_match:
        return json.loads(json_match.group())
    return {"tasks": []}


def find_day_column_blocks(notion, page_id, target_day):
    """ページ内のカラムレイアウトから該当曜日のブロックを探す"""
    blocks = notion.blocks.children.list(block_id=page_id)

    for block in blocks["results"]:
        if block["type"] == "column_list":
            columns = notion.blocks.children.list(block_id=block["id"])
            for column in columns["results"]:
                col_children = notion.blocks.children.list(block_id=column["id"])
                for child in col_children["results"]:
                    if child["type"] == "heading_2":
                        heading_text = ""
                        for rt in child["heading_2"].get("rich_text", []):
                            heading_text += rt.get("plain_text", "")
                        heading_text = heading_text.strip()
                        if heading_text == target_day:
                            return column["id"]
    return None


def append_tasks_to_column(notion, column_id, tasks):
    """カラムにタスクをToDoブロックとして追加する"""
    children = []
    for task in tasks:
        sub_blocks = []
        for subtask in task.get("subtasks", []):
            sub_blocks.append({
                "object": "block",
                "type": "to_do",
                "to_do": {
                    "rich_text": [{"type": "text", "text": {"content": subtask}}],
                    "checked": False,
                },
            })

        todo_block = {
            "object": "block",
            "type": "to_do",
            "to_do": {
                "rich_text": [{"type": "text", "text": {"content": task["title"]}}],
                "checked": False,
            },
        }
        children.append(todo_block)

        if sub_blocks:
            notion.blocks.children.append(block_id="placeholder", children=sub_blocks)

    if children:
        notion.blocks.children.append(block_id=column_id, children=children)

    return children


def append_tasks_with_subtasks(notion, column_id, tasks):
    """カラムにタスクとサブタスクをToDoブロックとして追加する"""
    for task in tasks:
        parent_result = notion.blocks.children.append(
            block_id=column_id,
            children=[{
                "object": "block",
                "type": "to_do",
                "to_do": {
                    "rich_text": [{"type": "text", "text": {"content": task["title"]}}],
                    "checked": False,
                },
            }],
        )

        parent_block_id = parent_result["results"][0]["id"]

        sub_blocks = []
        for subtask in task.get("subtasks", []):
            sub_blocks.append({
                "object": "block",
                "type": "to_do",
                "to_do": {
                    "rich_text": [{"type": "text", "text": {"content": subtask}}],
                    "checked": False,
                },
            })

        if sub_blocks:
            notion.blocks.children.append(
                block_id=parent_block_id,
                children=sub_blocks,
            )


@app.route("/api/process", methods=["POST"])
def process_voice_input():
    """音声入力テキストを処理してNotionに書き込む"""
    data = request.get_json()
    text = data.get("text", "")
    if not text:
        return jsonify({"error": "テキストが空です"}), 400

    target_date = datetime.now()

    # Geminiでタスク分解
    result = decompose_tasks_with_gemini(text, target_date)
    tasks = result.get("tasks", [])
    if not tasks:
        return jsonify({"error": "タスクを抽出できませんでした"}), 400

    # Notionに書き込み
    notion = NotionClient(auth=NOTION_TOKEN)
    page_id = find_weekly_page(notion, target_date)
    if not page_id:
        return jsonify({"error": "該当する週のNotionページが見つかりません"}), 404

    # 曜日ごとにタスクを振り分け
    tasks_by_day = {}
    for task in tasks:
        day = task.get("day", DAY_MAP[target_date.weekday()])
        if day not in tasks_by_day:
            tasks_by_day[day] = []
        tasks_by_day[day].append(task)

    written_days = []
    for day, day_tasks in tasks_by_day.items():
        column_id = find_day_column_blocks(notion, page_id, day)
        if column_id:
            append_tasks_with_subtasks(notion, column_id, day_tasks)
            written_days.append(day)

    return jsonify({
        "success": True,
        "tasks": tasks,
        "written_days": written_days,
        "message": f"{len(tasks)}個のタスクを{', '.join(written_days)}に書き込みました",
    })


@app.route("/api/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
