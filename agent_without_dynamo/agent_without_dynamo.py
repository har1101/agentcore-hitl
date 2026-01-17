"""
Strands Agents HITL + AgentCore Runtime - DynamoDB無しバージョン

メモリ内のみで状態を管理するシンプルなHITL実装。
interrupt発生時にcomplete_async_task()を呼ばず、HealthyBusy状態を維持することで
コンテナを存続させ、メモリ内の状態を保持する。

制約:
- 最大待機時間: 8時間(max_lifetime)
- コンテナ再起動で状態消失
- 待機中はメモリ課金あり(CPUは無料)
"""

import threading
import uuid
from datetime import datetime
from typing import Optional

from bedrock_agentcore.runtime.app import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import RequestContext
from strands import Agent, tool
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry

# ============================================================================
# 設定
# ============================================================================

DANGEROUS_TOOLS = ["delete_files", "execute_command", "modify_database"]

# ============================================================================
# メモリ内ストレージ
# ============================================================================

# セッション状態(エージェント、結果、ステータス)
session_states: dict[str, dict] = {}

# 承認待ちリクエスト(interrupt情報)
# session_id -> [approval_info, ...]
pending_approvals: dict[str, list[dict]] = {}


# ============================================================================
# 承認フック
# ============================================================================


class ApprovalHook(HookProvider):
    """危険なツール実行前に人間の承認を要求するフック"""

    def __init__(self, app_name: str, session_id: str):
        self.app_name = app_name
        self.session_id = session_id

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
    """ファイルを削除します(デモ用：実際には削除しません)"""
    return f"[DEMO] Would delete files: {paths}"


@tool
def execute_command(command: str) -> str:
    """コマンドを実行します(デモ用：実際には実行しません)"""
    return f"[DEMO] Would execute: {command}"


@tool
def modify_database(query: str) -> str:
    """データベースを変更します(デモ用：実際には変更しません)"""
    return f"[DEMO] Would execute SQL: {query}"


@tool
def list_files(directory: str = ".") -> str:
    """ディレクトリ内のファイル一覧を取得します(安全なツール)"""
    return f"Files in {directory}: file1.txt, file2.txt, file3.txt"


@tool
def read_file(path: str) -> str:
    """ファイルを読み取ります(安全なツール)"""
    return f"Content of {path}: Sample file content"


# ============================================================================
# AgentCore Runtime アプリケーション
# ============================================================================

app = BedrockAgentCoreApp(debug=True)


@app.entrypoint
def handler(payload: dict, context: RequestContext) -> dict:
    """メインエントリーポイント"""
    action = payload.get("action", "start")
    session_id = payload.get("session_id") or context.session_id or str(uuid.uuid4())

    print(f"[HITL] Action: {action}, Session: {session_id}")

    if action == "start":
        return start_agent_task(payload, session_id)
    elif action == "list_pending":
        return list_pending_approvals_handler(payload.get("filter_session_id"))
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

    # 非同期タスクを登録(HealthyBusy状態へ)
    task_id = app.add_async_task("agent_processing", {"session_id": session_id})
    print(f"[HITL] Started async task: {task_id}")

    # interruptが発生したかどうかを追跡
    interrupt_occurred = False

    def background_work():
        nonlocal interrupt_occurred
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
                interrupt_occurred = True
                print(f"[HITL] Agent interrupted, {len(result.interrupts)} approval(s) pending")

                # 承認待ち状態をメモリに保存
                for interrupt in result.interrupts:
                    pending_approvals.setdefault(session_id, []).append({
                        "session_id": session_id,
                        "interrupt_id": interrupt.id,
                        "name": interrupt.name,
                        "reason": interrupt.reason,
                        "status": "pending",
                        "created_at": datetime.now().isoformat(),
                    })
                    print(f"[HITL] Saved pending approval: {interrupt.id}")

                # セッション状態をメモリに保存(エージェントインスタンスも保持)
                session_states[session_id] = {
                    "agent": agent,
                    "result": result,
                    "status": "waiting_approval",
                    "prompt": prompt,
                    "task_id": task_id,  # 元のタスクIDを保存
                }

                # 重要: complete_async_task()を呼ばない
                # → HealthyBusy維持 → コンテナ存続 → メモリ保持
                print(f"[HITL] Staying HealthyBusy to preserve memory state")
                return  # finallyをスキップ

            else:
                # 正常完了
                print(f"[HITL] Agent completed: {result.stop_reason}")
                session_states[session_id] = {
                    "status": "completed",
                    "message": result.message,
                }
                # 完了したので承認待ちをクリア
                pending_approvals.pop(session_id, None)

        except Exception as e:
            print(f"[HITL] Agent error: {e}")
            import traceback
            traceback.print_exc()
            session_states[session_id] = {"status": "error", "error": str(e)}

        finally:
            # interruptが発生した場合はcomplete_async_task()を呼ばない
            if not interrupt_occurred:
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


def list_pending_approvals_handler(filter_session_id: Optional[str] = None) -> dict:
    """承認待ちリクエスト一覧を取得(メモリから)"""
    if filter_session_id:
        items = [
            a for a in pending_approvals.get(filter_session_id, [])
            if a.get("status") == "pending"
        ]
    else:
        items = [
            a for approvals in pending_approvals.values()
            for a in approvals
            if a.get("status") == "pending"
        ]

    return {
        "pending_approvals": items,
        "count": len(items),
    }


def approve_request(session_id: str, payload: dict) -> dict:
    """承認リクエストを処理(メモリ内の状態を更新)"""
    interrupt_id = payload.get("interrupt_id")
    response = payload.get("response", "y")
    approver = payload.get("approver", "cli")

    if not interrupt_id:
        return {"error": "interrupt_id is required"}

    # メモリ内の承認待ちを更新
    found = False
    for approval in pending_approvals.get(session_id, []):
        if approval["interrupt_id"] == interrupt_id:
            approval["status"] = "approved"
            approval["response"] = response
            approval["approver"] = approver
            approval["updated_at"] = datetime.now().isoformat()
            found = True
            break

    if not found:
        return {"error": f"Approval not found: {interrupt_id}"}

    print(f"[HITL] Approved: {interrupt_id} with response: {response}")

    return {
        "status": "approved",
        "session_id": session_id,
        "interrupt_id": interrupt_id,
        "response": response,
        "message": "Approval recorded. Use 'resume' to continue agent execution.",
    }


def reject_request(session_id: str, payload: dict) -> dict:
    """拒否リクエストを処理(メモリ内の状態を更新)"""
    interrupt_id = payload.get("interrupt_id")
    reason = payload.get("reason", "User rejected")
    approver = payload.get("approver", "cli")

    if not interrupt_id:
        return {"error": "interrupt_id is required"}

    # メモリ内の承認待ちを更新
    found = False
    for approval in pending_approvals.get(session_id, []):
        if approval["interrupt_id"] == interrupt_id:
            approval["status"] = "rejected"
            approval["response"] = "n"
            approval["approver"] = approver
            approval["rejection_reason"] = reason
            approval["updated_at"] = datetime.now().isoformat()
            found = True
            break

    if not found:
        return {"error": f"Approval not found: {interrupt_id}"}

    print(f"[HITL] Rejected: {interrupt_id}")

    return {
        "status": "rejected",
        "session_id": session_id,
        "interrupt_id": interrupt_id,
        "reason": reason,
        "message": "Rejection recorded. Use 'resume' to continue agent execution.",
    }


def resume_agent_task(session_id: str) -> dict:
    """承認後にエージェントタスクを再開(interruptResponse形式)"""
    state = session_states.get(session_id)

    if not state:
        return {"error": f"No session found: {session_id}. Session may have expired or container restarted."}

    if state.get("status") != "waiting_approval":
        return {"error": f"Session is not waiting for approval: {state.get('status')}"}

    agent = state.get("agent")
    prev_result = state.get("result")

    if not agent or not prev_result:
        return {"error": "Invalid session state: agent or result missing"}

    # 承認レスポンスを構築(interruptResponse形式)
    responses = []
    for interrupt in prev_result.interrupts:
        # メモリから承認状況を取得
        approval_response = None
        for approval in pending_approvals.get(session_id, []):
            if approval["interrupt_id"] == interrupt.id and approval["status"] in ["approved", "rejected"]:
                approval_response = approval.get("response")
                break

        if not approval_response:
            return {
                "error": f"Approval not found for interrupt: {interrupt.id}",
                "interrupt": {"id": interrupt.id, "name": interrupt.name},
            }

        responses.append({
            "interruptResponse": {
                "interruptId": interrupt.id,
                "response": approval_response,
            }
        })

    # 元のタスクIDを取得(新しいタスクは作らない)
    task_id = state.get("task_id")
    if not task_id:
        return {"error": "No task_id found in session state"}

    # interruptが発生したかどうかを追跡
    interrupt_occurred = False

    def resume_work():
        nonlocal interrupt_occurred
        try:
            # interruptResponse形式でエージェントを再開
            print(f"[HITL] Resuming agent with {len(responses)} response(s)")
            result = agent(responses)

            if result.stop_reason == "interrupt":
                # 新しいinterruptが発生
                interrupt_occurred = True
                print(f"[HITL] New interrupt occurred, {len(result.interrupts)} approval(s) pending")

                for interrupt in result.interrupts:
                    pending_approvals.setdefault(session_id, []).append({
                        "session_id": session_id,
                        "interrupt_id": interrupt.id,
                        "name": interrupt.name,
                        "reason": interrupt.reason,
                        "status": "pending",
                        "created_at": datetime.now().isoformat(),
                    })

                session_states[session_id] = {
                    "agent": agent,
                    "result": result,
                    "status": "waiting_approval",
                    "prompt": state.get("prompt"),
                    "task_id": task_id,  # 元のタスクIDを引き継ぐ
                }

                # complete_async_task()を呼ばない
                print(f"[HITL] Staying HealthyBusy for new interrupt")
                return

            else:
                # 正常完了
                print(f"[HITL] Agent completed: {result.stop_reason}")
                session_states[session_id] = {
                    "status": "completed",
                    "message": result.message,
                }
                # 承認待ちリストをクリア
                pending_approvals.pop(session_id, None)

        except Exception as e:
            print(f"[HITL] Resume error: {e}")
            import traceback
            traceback.print_exc()
            session_states[session_id] = {"status": "error", "error": str(e)}

        finally:
            if not interrupt_occurred:
                app.complete_async_task(task_id)
                print(f"[HITL] Completed async task: {task_id}")

    thread = threading.Thread(target=resume_work, daemon=True)
    thread.start()

    return {
        "status": "resuming",
        "session_id": session_id,
        "task_id": task_id,
        "message": "Agent resuming with approval responses.",
    }


def get_result(session_id: str) -> dict:
    """エージェントの実行結果を取得(メモリから)"""
    state = session_states.get(session_id)

    if state:
        response = {"session_id": session_id, "status": state.get("status")}
        if state.get("message"):
            response["message"] = state.get("message")
        if state.get("error"):
            response["error"] = state.get("error")
        return response

    return {"error": f"No result found for session: {session_id}. Session may have expired or container restarted."}


def get_status(session_id: str) -> dict:
    """セッションの状態を取得"""
    state = session_states.get(session_id)

    if state:
        return {
            "session_id": session_id,
            "status": state.get("status"),
            "has_agent": state.get("agent") is not None,
        }

    return {"error": f"Session not found: {session_id}. Session may have expired or container restarted."}


if __name__ == "__main__":
    app.run()
