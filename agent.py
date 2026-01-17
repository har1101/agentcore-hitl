"""
Strands Agents HITL + AgentCore Runtime 非同期実行

このモジュールは、Strands AgentsのInterrupts機能とAgentCore Runtimeの
非同期実行を組み合わせて、バックグラウンド実行 + Human in the Loopを実現します。
"""

import os
import json
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

import boto3
from boto3.dynamodb.conditions import Key, Attr
from bedrock_agentcore.runtime.app import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import RequestContext
from strands import Agent, tool
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry
from strands.types.tools import ToolContext

# ============================================================================
# 設定
# ============================================================================

DYNAMODB_TABLE_NAME = os.environ.get("HITL_TABLE_NAME", "hitl-approvals")
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
DANGEROUS_TOOLS = ["delete_files", "execute_command", "modify_database"]

# ============================================================================
# DynamoDB クライアント
# ============================================================================

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table(DYNAMODB_TABLE_NAME)


def save_pending_approval(session_id: str, interrupt_id: str, name: str, reason: dict):
    """承認待ちリクエストをDynamoDBに保存"""
    ttl = int((datetime.now() + timedelta(days=7)).timestamp())
    item = {
        "session_id": session_id,
        "interrupt_id": interrupt_id,
        "name": name,
        "reason": json.dumps(reason, ensure_ascii=False),
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "ttl": ttl,
    }
    table.put_item(Item=item)
    return item


def _convert_decimals(obj):
    """DynamoDB Decimal を JSON シリアライズ可能な型に変換"""
    from decimal import Decimal
    if isinstance(obj, dict):
        return {k: _convert_decimals(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_decimals(item) for item in obj]
    elif isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


def get_pending_approvals(session_id: Optional[str] = None):
    """承認待ちリクエスト一覧を取得"""
    if session_id:
        response = table.query(
            KeyConditionExpression=Key("session_id").eq(session_id),
            FilterExpression=Attr("status").eq("pending"),
        )
    else:
        response = table.query(
            IndexName="status-index",
            KeyConditionExpression=Key("status").eq("pending"),
        )
    items = response.get("Items", [])
    for item in items:
        if "reason" in item:
            item["reason"] = json.loads(item["reason"])
    # Decimal を int/float に変換
    return _convert_decimals(items)


def update_approval_status(session_id: str, interrupt_id: str, status: str, response: str, approver: str = "cli"):
    """承認状態を更新"""
    table.update_item(
        Key={"session_id": session_id, "interrupt_id": interrupt_id},
        UpdateExpression="SET #status = :status, #response = :response, #approver = :approver, #updated_at = :updated_at",
        ExpressionAttributeNames={
            "#status": "status",
            "#response": "response",
            "#approver": "approver",
            "#updated_at": "updated_at",
        },
        ExpressionAttributeValues={
            ":status": status,
            ":response": response,
            ":approver": approver,
            ":updated_at": datetime.now().isoformat(),
        },
    )


def get_approval_response(session_id: str, interrupt_id: str) -> Optional[str]:
    """承認レスポンスを取得"""
    response = table.get_item(Key={"session_id": session_id, "interrupt_id": interrupt_id})
    item = response.get("Item")
    if item and item.get("status") in ["approved", "rejected"]:
        return item.get("response")
    return None


def save_agent_result(session_id: str, result: dict):
    """エージェントの実行結果を保存"""
    ttl = int((datetime.now() + timedelta(days=7)).timestamp())
    table.put_item(
        Item={
            "session_id": session_id,
            "interrupt_id": "__result__",
            "status": "completed",
            "result": json.dumps(result, ensure_ascii=False),
            "created_at": datetime.now().isoformat(),
            "ttl": ttl,
        }
    )


def get_agent_result(session_id: str) -> Optional[dict]:
    """エージェントの実行結果を取得"""
    response = table.get_item(Key={"session_id": session_id, "interrupt_id": "__result__"})
    item = response.get("Item")
    if item:
        return json.loads(item.get("result", "{}"))
    return None


def save_agent_state(session_id: str, state: dict):
    """エージェントの状態を保存（再開用）"""
    ttl = int((datetime.now() + timedelta(days=7)).timestamp())
    table.put_item(
        Item={
            "session_id": session_id,
            "interrupt_id": "__state__",
            "status": "interrupted",
            "state": json.dumps(state, ensure_ascii=False),
            "created_at": datetime.now().isoformat(),
            "ttl": ttl,
        }
    )


def get_agent_state(session_id: str) -> Optional[dict]:
    """エージェントの状態を取得（再開用）"""
    response = table.get_item(Key={"session_id": session_id, "interrupt_id": "__state__"})
    item = response.get("Item")
    if item:
        return json.loads(item.get("state", "{}"))
    return None


# ============================================================================
# 承認フック
# ============================================================================


class ApprovalHook(HookProvider):
    """危険なツール実行前に人間の承認を要求するフック"""

    def __init__(self, app_name: str, session_id: str, pre_approved_tools: dict[str, str] = None):
        self.app_name = app_name
        self.session_id = session_id
        # 事前承認されたツール: {tool_name: response}
        self.pre_approved_tools = pre_approved_tools or {}

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self.approve)

    def approve(self, event: BeforeToolCallEvent) -> None:
        tool_name = event.tool_use["name"]

        if tool_name not in DANGEROUS_TOOLS:
            return

        # 既に信頼済みならスキップ
        trust_key = f"{self.app_name}-{tool_name}-trust"
        if event.agent.state.get(trust_key) == "trusted":
            print(f"[HITL] Tool '{tool_name}' is trusted, skipping approval")
            return

        # 事前承認がある場合はそれを使用（再開時）
        if tool_name in self.pre_approved_tools:
            approval = self.pre_approved_tools[tool_name]
            print(f"[HITL] Using pre-approved response for tool '{tool_name}': {approval}")
            if approval.lower() == "t":
                event.agent.state.set(trust_key, "trusted")
                print(f"[HITL] Tool '{tool_name}' is now trusted")
            elif approval.lower() != "y":
                event.cancel_tool = f"User denied execution of '{tool_name}'"
                print(f"[HITL] Tool '{tool_name}' execution denied (pre-approved)")
            else:
                print(f"[HITL] Tool '{tool_name}' approved for single execution (pre-approved)")
            return

        # 承認を要求
        print(f"[HITL] Requesting approval for tool: {tool_name}")
        approval = event.interrupt(
            f"{self.app_name}-{tool_name}-approval",
            reason={
                "tool": tool_name,
                "input": event.tool_use["input"],
                "message": f"Tool '{tool_name}' requires human approval before execution",
            },
        )

        # 承認結果を処理
        if approval.lower() == "t":  # trust - 今後は承認不要
            event.agent.state.set(trust_key, "trusted")
            print(f"[HITL] Tool '{tool_name}' is now trusted")
        elif approval.lower() != "y":
            event.cancel_tool = f"User denied execution of '{tool_name}'"
            print(f"[HITL] Tool '{tool_name}' execution denied")
        else:
            print(f"[HITL] Tool '{tool_name}' approved for single execution")


# ============================================================================
# サンプルツール
# ============================================================================


@tool
def delete_files(paths: list[str]) -> str:
    """ファイルを削除します（デモ用：実際には削除しません）"""
    return f"[DEMO] Would delete files: {paths}"


@tool
def execute_command(command: str) -> str:
    """コマンドを実行します（デモ用：実際には実行しません）"""
    return f"[DEMO] Would execute: {command}"


@tool
def modify_database(query: str) -> str:
    """データベースを変更します（デモ用：実際には変更しません）"""
    return f"[DEMO] Would execute SQL: {query}"


@tool
def list_files(directory: str = ".") -> str:
    """ディレクトリ内のファイル一覧を取得します（安全なツール）"""
    return f"Files in {directory}: file1.txt, file2.txt, file3.txt"


@tool
def read_file(path: str) -> str:
    """ファイルを読み取ります（安全なツール）"""
    return f"Content of {path}: Sample file content"


# ============================================================================
# AgentCore Runtime アプリケーション
# ============================================================================

app = BedrockAgentCoreApp(debug=True)

# セッションごとの状態を保持（メモリ内キャッシュ）
session_states: dict[str, dict] = {}


@app.entrypoint
def handler(payload: dict, context: RequestContext) -> dict:
    """メインエントリーポイント"""
    action = payload.get("action", "start")
    session_id = payload.get("session_id") or context.session_id or str(uuid.uuid4())

    print(f"[HITL] Action: {action}, Session: {session_id}")

    if action == "start":
        return start_agent_task(payload, session_id)
    elif action == "list_pending":
        return list_pending_approvals(payload.get("filter_session_id"))
    elif action == "approve":
        return approve_request(session_id, payload)
    elif action == "reject":
        return reject_request(session_id, payload)
    elif action == "resume":
        return resume_agent_task(session_id)
    elif action == "result":
        return get_result(session_id)
    elif action == "status":
        return get_status(session_id)
    else:
        return {"error": f"Unknown action: {action}"}


def start_agent_task(payload: dict, session_id: str) -> dict:
    """エージェントタスクをバックグラウンドで開始"""
    prompt = payload.get("prompt", "Hello!")

    # 非同期タスクを登録
    task_id = app.add_async_task("agent_processing", {"session_id": session_id})
    print(f"[HITL] Started async task: {task_id}")

    def background_work():
        try:
            # エージェントを作成
            agent = Agent(
                model="jp.anthropic.claude-haiku-4-5-20251001-v1:0",
                hooks=[ApprovalHook("hitl-demo", session_id)],
                tools=[delete_files, execute_command, modify_database, list_files, read_file],
                system_prompt="""You are a helpful assistant that can manage files and execute commands.
When asked to delete files or execute commands, use the appropriate tools.
Always confirm what you will do before taking action.""",
            )

            # エージェントを実行
            result = agent(prompt)

            # Interruptが発生した場合
            if result.stop_reason == "interrupt":
                print(f"[HITL] Agent interrupted, {len(result.interrupts)} approval(s) pending")

                # 承認待ち状態を保存
                for interrupt in result.interrupts:
                    save_pending_approval(
                        session_id=session_id,
                        interrupt_id=interrupt.id,
                        name=interrupt.name,
                        reason=interrupt.reason,
                    )
                    print(f"[HITL] Saved pending approval: {interrupt.id}")

                # エージェントの状態を保存（再開用）
                save_agent_state(
                    session_id=session_id,
                    state={
                        "prompt": prompt,
                        "interrupts": [
                            {"id": i.id, "name": i.name, "reason": i.reason}
                            for i in result.interrupts
                        ],
                    },
                )

                # メモリ内にも保持
                session_states[session_id] = {
                    "agent": agent,
                    "result": result,
                    "status": "waiting_approval",
                }
            else:
                # 正常完了
                print(f"[HITL] Agent completed: {result.stop_reason}")
                save_agent_result(session_id, {"message": result.message, "stop_reason": result.stop_reason})
                session_states[session_id] = {"status": "completed", "message": result.message}

        except Exception as e:
            print(f"[HITL] Agent error: {e}")
            save_agent_result(session_id, {"error": str(e)})
            session_states[session_id] = {"status": "error", "error": str(e)}

        finally:
            # 非同期タスクを完了
            app.complete_async_task(task_id)
            print(f"[HITL] Completed async task: {task_id}")

    # バックグラウンドスレッドで実行
    thread = threading.Thread(target=background_work, daemon=True)
    thread.start()

    return {
        "status": "started",
        "session_id": session_id,
        "task_id": task_id,
        "message": "Agent task started in background. Use 'list_pending' to check for approval requests.",
    }


def list_pending_approvals(filter_session_id: Optional[str] = None) -> dict:
    """承認待ちリクエスト一覧を取得"""
    items = get_pending_approvals(filter_session_id)
    return {
        "pending_approvals": items,
        "count": len(items),
    }


def approve_request(session_id: str, payload: dict) -> dict:
    """承認リクエストを処理"""
    interrupt_id = payload.get("interrupt_id")
    response = payload.get("response", "y")
    approver = payload.get("approver", "cli")

    if not interrupt_id:
        return {"error": "interrupt_id is required"}

    update_approval_status(session_id, interrupt_id, "approved", response, approver)
    print(f"[HITL] Approved: {interrupt_id}")

    return {
        "status": "approved",
        "session_id": session_id,
        "interrupt_id": interrupt_id,
        "response": response,
        "message": "Approval recorded. Use 'resume' to continue agent execution.",
    }


def reject_request(session_id: str, payload: dict) -> dict:
    """拒否リクエストを処理"""
    interrupt_id = payload.get("interrupt_id")
    reason = payload.get("reason", "User rejected")
    approver = payload.get("approver", "cli")

    if not interrupt_id:
        return {"error": "interrupt_id is required"}

    update_approval_status(session_id, interrupt_id, "rejected", "n", approver)
    print(f"[HITL] Rejected: {interrupt_id}")

    return {
        "status": "rejected",
        "session_id": session_id,
        "interrupt_id": interrupt_id,
        "reason": reason,
        "message": "Rejection recorded. Use 'resume' to continue agent execution.",
    }


def resume_agent_task(session_id: str) -> dict:
    """承認後にエージェントタスクを再開"""
    # メモリ内の状態を確認
    state = session_states.get(session_id)

    if not state:
        # DynamoDBから状態を復元
        saved_state = get_agent_state(session_id)
        if not saved_state:
            return {"error": f"No pending session found: {session_id}"}

        # 承認状況を確認し、ツール名 -> 承認レスポンスのマッピングを構築
        interrupts = saved_state.get("interrupts", [])
        pre_approved_tools: dict[str, str] = {}
        pending_interrupts = []

        for interrupt in interrupts:
            approval_response = get_approval_response(session_id, interrupt["id"])
            if approval_response:
                # reasonからツール名を取得
                reason = interrupt.get("reason", {})
                tool_name = reason.get("tool") if isinstance(reason, dict) else None
                if tool_name:
                    pre_approved_tools[tool_name] = approval_response
                    print(f"[HITL] Pre-approved tool: {tool_name} -> {approval_response}")
            else:
                pending_interrupts.append(interrupt)

        if pending_interrupts:
            return {
                "error": "Not all approvals have been responded to",
                "pending": pending_interrupts,
            }

        # 元のプロンプトを取得
        original_prompt = saved_state.get("prompt", "")
        if not original_prompt:
            return {"error": "Original prompt not found in saved state"}

        # エージェントを再作成して元のプロンプトを再実行（事前承認付き）
        task_id = app.add_async_task("agent_resume", {"session_id": session_id})

        def resume_work():
            try:
                # 事前承認されたツールを渡してエージェントを作成
                agent = Agent(
                    model="jp.anthropic.claude-haiku-4-5-20251001-v1:0",
                    hooks=[ApprovalHook("hitl-demo", session_id, pre_approved_tools=pre_approved_tools)],
                    tools=[delete_files, execute_command, modify_database, list_files, read_file],
                    system_prompt="""You are a helpful assistant that can manage files and execute commands.
When asked to delete files or execute commands, use the appropriate tools.
Always confirm what you will do before taking action.""",
                )

                # 元のプロンプトを再実行（事前承認が適用される）
                print(f"[HITL] Re-running prompt with pre-approved tools: {list(pre_approved_tools.keys())}")
                result = agent(original_prompt)

                if result.stop_reason == "interrupt":
                    # 新しいInterruptが発生（別のツール）
                    for interrupt in result.interrupts:
                        save_pending_approval(session_id, interrupt.id, interrupt.name, interrupt.reason)
                    save_agent_state(
                        session_id,
                        {
                            "prompt": original_prompt,
                            "interrupts": [{"id": i.id, "name": i.name, "reason": i.reason} for i in result.interrupts],
                        },
                    )
                    session_states[session_id] = {"agent": agent, "result": result, "status": "waiting_approval"}
                else:
                    save_agent_result(session_id, {"message": result.message, "stop_reason": result.stop_reason})
                    session_states[session_id] = {"status": "completed", "message": result.message}

            except Exception as e:
                print(f"[HITL] Resume error: {e}")
                import traceback
                traceback.print_exc()
                save_agent_result(session_id, {"error": str(e)})

            finally:
                app.complete_async_task(task_id)

        thread = threading.Thread(target=resume_work, daemon=True)
        thread.start()

        return {
            "status": "resuming",
            "session_id": session_id,
            "task_id": task_id,
            "pre_approved_tools": list(pre_approved_tools.keys()),
            "message": "Agent resuming by re-running prompt with pre-approved tools.",
        }

    # メモリ内に状態がある場合
    if state.get("status") != "waiting_approval":
        return {"error": f"Session is not waiting for approval: {state.get('status')}"}

    agent = state.get("agent")
    prev_result = state.get("result")

    if not agent or not prev_result:
        return {"error": "Invalid session state"}

    # 承認状況を確認
    responses = []
    for interrupt in prev_result.interrupts:
        approval_response = get_approval_response(session_id, interrupt.id)
        if not approval_response:
            return {
                "error": f"Approval not found for interrupt: {interrupt.id}",
                "interrupt": {"id": interrupt.id, "name": interrupt.name, "reason": interrupt.reason},
            }
        responses.append(
            {
                "interruptResponse": {
                    "interruptId": interrupt.id,
                    "response": approval_response,
                }
            }
        )

    # 非同期タスクを登録して再開
    task_id = app.add_async_task("agent_resume", {"session_id": session_id})

    def resume_work():
        try:
            result = agent(responses)

            if result.stop_reason == "interrupt":
                for interrupt in result.interrupts:
                    save_pending_approval(session_id, interrupt.id, interrupt.name, interrupt.reason)
                session_states[session_id] = {"agent": agent, "result": result, "status": "waiting_approval"}
            else:
                save_agent_result(session_id, {"message": result.message, "stop_reason": result.stop_reason})
                session_states[session_id] = {"status": "completed", "message": result.message}

        except Exception as e:
            print(f"[HITL] Resume error: {e}")
            save_agent_result(session_id, {"error": str(e)})

        finally:
            app.complete_async_task(task_id)

    thread = threading.Thread(target=resume_work, daemon=True)
    thread.start()

    return {
        "status": "resuming",
        "session_id": session_id,
        "task_id": task_id,
        "message": "Agent resuming with approval responses.",
    }


def get_result(session_id: str) -> dict:
    """エージェントの実行結果を取得"""
    # メモリ内の状態を確認
    state = session_states.get(session_id)
    if state:
        response = {"session_id": session_id, "status": state.get("status")}
        # None以外の値のみ含める
        if state.get("message"):
            response["message"] = state.get("message")
        if state.get("error"):
            response["error"] = state.get("error")
        return response

    # DynamoDBから結果を取得
    result = get_agent_result(session_id)
    if result:
        return {"session_id": session_id, "result": result}

    return {"error": f"No result found for session: {session_id}"}


def get_status(session_id: str) -> dict:
    """セッションの状態を取得"""
    # メモリ内の状態を確認
    state = session_states.get(session_id)
    if state:
        return {
            "session_id": session_id,
            "status": state.get("status"),
            "in_memory": True,
        }

    # DynamoDBから状態を確認
    saved_state = get_agent_state(session_id)
    if saved_state:
        return {
            "session_id": session_id,
            "status": "interrupted",
            "in_memory": False,
            "interrupts": saved_state.get("interrupts", []),
        }

    result = get_agent_result(session_id)
    if result:
        return {
            "session_id": session_id,
            "status": "completed" if "message" in result else "error",
            "in_memory": False,
        }

    return {"error": f"Session not found: {session_id}"}


if __name__ == "__main__":
    app.run()
