"""
Strands Agents HITL + AgentCore Runtime - Streamlit UI

ローカルから AWS上のAgentCore Runtimeを呼び出し、
HITLワークフローを管理するWebインターフェース
"""

import json
import streamlit as st
from datetime import datetime, timezone, timedelta

import boto3

# ========================================
# ユーティリティ
# ========================================

JST = timezone(timedelta(hours=9))


def utc_to_jst(utc_str: str) -> str:
    """UTC時間文字列をJST表示用文字列に変換"""
    if not utc_str:
        return ""
    try:
        # ISO形式をパース（例: "2026-01-16T03:06:22.129454"）
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        # タイムゾーン情報がない場合はUTCとして扱う
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # JSTに変換
        jst_dt = dt.astimezone(JST)
        return jst_dt.strftime("%Y-%m-%d %H:%M:%S JST")
    except (ValueError, TypeError):
        return utc_str  # パース失敗時は元の文字列を返す

# ========================================
# 設定
# ========================================
AWS_REGION = "ap-northeast-1"
AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:ap-northeast-1:975050047634:runtime/agent-iXxA6XCZ6b"

# ========================================
# AgentCore SDK クライアント
# ========================================

def get_agentcore_client():
    """AgentCore クライアントを取得"""
    return boto3.client("bedrock-agentcore", region_name=AWS_REGION)


def invoke_agentcore(payload: dict, session_id: str = None) -> dict:
    """SDK を使用してエージェントを呼び出す"""
    from decimal import Decimal
    client = get_agentcore_client()

    try:
        kwargs = {
            "agentRuntimeArn": AGENT_RUNTIME_ARN,
            "payload": json.dumps(payload),
            "qualifier": "DEFAULT",
        }
        if session_id:
            kwargs["runtimeSessionId"] = session_id

        response = client.invoke_agent_runtime(**kwargs)

        # ストリームを.read()で読み取る
        response_body = response["response"].read()
        raw_content = response_body.decode("utf-8") if isinstance(response_body, bytes) else str(response_body)

        if not raw_content:
            return {"error": "Empty response"}

        # JSONパースを試行
        try:
            return json.loads(raw_content)
        except json.JSONDecodeError:
            pass

        # Python literal (Decimal含む) としてパース（フォールバック）
        try:
            result = eval(raw_content, {"Decimal": Decimal, "__builtins__": {}})
            return _convert_decimals(result)
        except Exception as e:
            return {"error": f"Parse failed: {e}", "raw": raw_content[:500]}

    except Exception as e:
        return {"error": str(e)}


def _convert_decimals(obj):
    """Decimal を int/float に変換"""
    from decimal import Decimal
    if isinstance(obj, dict):
        return {k: _convert_decimals(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_decimals(item) for item in obj]
    elif isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


# ========================================
# Streamlit UI
# ========================================

st.set_page_config(page_title="HITL Approval Dashboard", layout="wide")
st.title("🤖 Human in the Loop - 承認ダッシュボード")

# サイドバー
with st.sidebar:
    st.header("エージェント設定")
    st.text(f"Region: {AWS_REGION}")
    st.text(f"ARN: .../{AGENT_RUNTIME_ARN.split('/')[-1]}")

    if st.button("🔄 承認待ち一覧を更新"):
        st.rerun()

# タブ
tab1, tab2, tab3 = st.tabs(["🚀 タスク開始", "📋 承認待ち一覧", "📊 結果確認"])

# ========================================
# タブ1: タスク開始
# ========================================
with tab1:
    st.header("新しいタスクを開始")

    prompt = st.text_area(
        "プロンプト", value="Please delete the file /tmp/test.txt", height=100
    )

    if st.button("▶️ タスク開始", type="primary"):
        with st.spinner("タスクを開始中..."):
            result = invoke_agentcore({"action": "start", "prompt": prompt})

        if "error" in result:
            st.error(f"エラー: {result['error']}")
        elif "session_id" in result:
            st.success("タスクが開始されました！")
            st.json(result)
            # セッションIDを保存
            if "sessions" not in st.session_state:
                st.session_state.sessions = []
            st.session_state.sessions.append(
                {
                    "session_id": result["session_id"],
                    "prompt": prompt[:50] + "...",
                    "created_at": datetime.now().isoformat(),
                }
            )
        else:
            st.warning("予期しないレスポンス:")
            st.json(result)

# ========================================
# タブ2: 承認待ち一覧
# ========================================
with tab2:
    st.header("承認待ちリクエスト")

    # 承認待ち一覧を取得
    pending_result = invoke_agentcore({"action": "list_pending"})

    # 結果が辞書でない場合はエラー表示
    if not isinstance(pending_result, dict):
        st.error(f"予期しないレスポンス型: {type(pending_result).__name__}")
        st.code(str(pending_result)[:500])
    elif "error" in pending_result:
        st.error(f"エラー: {pending_result['error']}")
        if "traceback" in pending_result:
            st.code(pending_result["traceback"])
    elif "pending_approvals" in pending_result:
        approvals = pending_result["pending_approvals"]

        if not approvals:
            st.info("承認待ちのリクエストはありません")
        else:
            st.write(f"**{len(approvals)} 件の承認待ち**")

            for i, approval in enumerate(approvals):
                created_at_jst = utc_to_jst(approval.get('created_at', ''))
                with st.expander(
                    f"🔔 {approval.get('name', 'Unknown')} - {created_at_jst}",
                    expanded=True,
                ):
                    col1, col2 = st.columns([3, 1])

                    with col1:
                        st.write("**セッションID:**", approval.get("session_id", "N/A"))
                        st.write(
                            "**Interrupt ID:**", approval.get("interrupt_id", "N/A")
                        )

                        reason = approval.get("reason", {})
                        if isinstance(reason, str):
                            reason = json.loads(reason)

                        st.write("**ツール:**", reason.get("tool", "N/A"))
                        st.write("**入力パラメータ:**")
                        st.json(reason.get("input", {}))
                        st.write("**メッセージ:**", reason.get("message", "N/A"))

                    with col2:
                        session_id = approval.get("session_id")
                        interrupt_id = approval.get("interrupt_id")

                        # 承認ボタン
                        if st.button("✅ 承認", key=f"approve_{i}", type="primary"):
                            with st.spinner("承認処理中..."):
                                approve_result = invoke_agentcore(
                                    {
                                        "action": "approve",
                                        "session_id": session_id,
                                        "interrupt_id": interrupt_id,
                                        "response": "y",
                                    }
                                )
                            if (
                                "status" in approve_result
                                and approve_result["status"] == "approved"
                            ):
                                st.success("承認しました！")
                                # 自動で再開
                                with st.spinner("エージェントを再開中..."):
                                    resume_result = invoke_agentcore(
                                        {"action": "resume", "session_id": session_id},
                                        session_id=session_id,
                                    )
                                st.info("エージェントを再開しました")
                                st.rerun()
                            else:
                                st.error(f"承認エラー: {approve_result}")

                        # 信頼ボタン（今後も自動承認）
                        if st.button("🔒 信頼", key=f"trust_{i}"):
                            with st.spinner("信頼処理中..."):
                                trust_result = invoke_agentcore(
                                    {
                                        "action": "approve",
                                        "session_id": session_id,
                                        "interrupt_id": interrupt_id,
                                        "response": "t",  # trust
                                    }
                                )
                            if "status" in trust_result:
                                st.success("このツールを信頼しました！")
                                st.rerun()

                        # 拒否ボタン
                        if st.button("❌ 拒否", key=f"reject_{i}"):
                            with st.spinner("拒否処理中..."):
                                reject_result = invoke_agentcore(
                                    {
                                        "action": "reject",
                                        "session_id": session_id,
                                        "interrupt_id": interrupt_id,
                                        "reason": "User rejected via UI",
                                    }
                                )
                            if "status" in reject_result:
                                st.warning("拒否しました")
                                st.rerun()
    else:
        st.warning("予期しないレスポンス:")
        st.json(pending_result)

# ========================================
# タブ3: 結果確認
# ========================================
with tab3:
    st.header("タスク結果")

    # セッションID入力
    session_id_input = st.text_input("セッションID", placeholder="abc123-...")

    # 保存されたセッション一覧
    if "sessions" in st.session_state and st.session_state.sessions:
        st.write("**最近のセッション:**")
        for session in reversed(st.session_state.sessions[-5:]):
            if st.button(
                f"📝 {session['session_id'][:16]}... ({session['prompt']})",
                key=f"session_{session['session_id']}",
            ):
                session_id_input = session["session_id"]

    if st.button("🔍 結果を取得") and session_id_input:
        with st.spinner("結果を取得中..."):
            result = invoke_agentcore(
                {"action": "result", "session_id": session_id_input},
                session_id=session_id_input,
            )

        if "error" in result:
            st.error(f"エラー: {result['error']}")
        elif "result" in result or "message" in result:
            st.success("結果を取得しました！")
            st.json(result)
        elif "status" in result:
            st.info(f"ステータス: {result['status']}")
            st.json(result)
        else:
            st.warning("予期しないレスポンス:")
            st.json(result)

# フッター
st.divider()
st.caption("Strands Agents HITL + AgentCore Runtime Demo")
