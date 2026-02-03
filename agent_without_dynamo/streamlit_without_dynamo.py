"""
Strands Agents HITL + AgentCore Runtime - Streamlit UI (DynamoDB無しバージョン)

ローカルまたはAWS上のAgentCore Runtimeを呼び出し、
HITLワークフローを管理するWebインターフェース。

DynamoDB無しバージョンに対応:
- ローカルモード: http://localhost:9080 を直接呼び出し
- AWSモード: boto3 SDKでAgentCore Runtimeを呼び出し
"""

import json
import streamlit as st
from datetime import datetime, timezone, timedelta

# ========================================
# 設定
# ========================================

# モード切替（Trueでローカル、FalseでAWS）
LOCAL_MODE = False
LOCAL_ENDPOINT = "http://localhost:9080"

# AWS デプロイ用（LOCAL_MODE = False の場合に使用）
AWS_REGION = "ap-northeast-1"
AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:ap-northeast-1:xxxxxxxxxxxx:runtime/<Runtime ID>"  # デプロイ後に設定

# ========================================
# ユーティリティ
# ========================================

JST = timezone(timedelta(hours=9))


def utc_to_jst(utc_str: str) -> str:
    """UTC時間文字列をJST表示用文字列に変換"""
    if not utc_str:
        return ""
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        jst_dt = dt.astimezone(JST)
        return jst_dt.strftime("%Y-%m-%d %H:%M:%S JST")
    except (ValueError, TypeError):
        return utc_str


# ========================================
# AgentCore 呼び出し
# ========================================


def invoke_agentcore(payload: dict, session_id: str = None) -> dict:
    """AgentCoreを呼び出す（モードに応じて切替）"""
    if LOCAL_MODE:
        return invoke_local(payload, session_id)
    else:
        return invoke_runtime(payload, session_id)


def invoke_local(payload: dict, session_id: str = None) -> dict:
    """ローカルエンドポイントを呼び出す"""
    import requests

    try:
        # セッションIDがある場合はpayloadに含める
        if session_id and "session_id" not in payload:
            payload["session_id"] = session_id

        response = requests.post(
            f"{LOCAL_ENDPOINT}/invocations",
            json=payload,
            timeout=120,
        )
        return response.json()
    except requests.exceptions.ConnectionError:
        return {"error": f"接続エラー: {LOCAL_ENDPOINT} に接続できません。agentcore launch --local を実行してください。"}
    except requests.exceptions.Timeout:
        return {"error": "タイムアウト: リクエストがタイムアウトしました"}
    except Exception as e:
        return {"error": str(e)}


def invoke_runtime(payload: dict, session_id: str = None) -> dict:
    """AWS AgentCore Runtimeを呼び出す（boto3 SDK）"""
    import boto3
    from decimal import Decimal

    if not AGENT_RUNTIME_ARN:
        return {"error": "AGENT_RUNTIME_ARN が設定されていません"}

    try:
        client = boto3.client("bedrock-agentcore", region_name=AWS_REGION)

        kwargs = {
            "agentRuntimeArn": AGENT_RUNTIME_ARN,
            "payload": json.dumps(payload),
            "qualifier": "DEFAULT",
        }
        if session_id:
            kwargs["runtimeSessionId"] = session_id

        response = client.invoke_agent_runtime(**kwargs)

        response_body = response["response"].read()
        raw_content = response_body.decode("utf-8") if isinstance(response_body, bytes) else str(response_body)

        # デバッグ: raw_contentをログ出力
        print(f"[DEBUG] raw_content type: {type(raw_content)}")
        print(f"[DEBUG] raw_content (first 500 chars): {raw_content[:500]}")

        if not raw_content:
            return {"error": "Empty response"}

        try:
            parsed = json.loads(raw_content)
            print(f"[DEBUG] JSON parsed successfully: {type(parsed)}")
            return parsed
        except json.JSONDecodeError as e:
            print(f"[DEBUG] JSON parse failed: {e}")
            pass

        try:
            result = eval(raw_content, {"Decimal": Decimal, "__builtins__": {}})
            print(f"[DEBUG] eval parsed successfully: {type(result)}")
            return _convert_decimals(result)
        except Exception as e:
            print(f"[DEBUG] eval failed: {e}")
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

st.set_page_config(page_title="HITL Approval Dashboard (No DynamoDB)", layout="wide")
st.title("🤖 Human in the Loop - 承認ダッシュボード")
st.caption("DynamoDB無しバージョン（メモリ内状態管理）")

# セッション状態の初期化
if "sessions" not in st.session_state:
    st.session_state.sessions = []
if "selected_session_id" not in st.session_state:
    st.session_state.selected_session_id = None


def get_session_options() -> list[tuple[str, str]]:
    """セッション選択肢を取得（session_id, 表示ラベル）"""
    options = []
    for session in reversed(st.session_state.sessions[-10:]):  # 最新10件
        label = f"{session['session_id'][:8]}... ({session['prompt'][:20]}...)"
        options.append((session["session_id"], label))
    return options


# サイドバー
with st.sidebar:
    st.header("設定")

    if LOCAL_MODE:
        st.success("🟢 ローカルモード")
        st.text(f"Endpoint: {LOCAL_ENDPOINT}")
    else:
        st.info("☁️ AWSモード")
        st.text(f"Region: {AWS_REGION}")
        if AGENT_RUNTIME_ARN:
            st.text(f"ARN: .../{AGENT_RUNTIME_ARN.split('/')[-1]}")
        else:
            st.warning("ARN未設定")

    st.divider()

    # セッション管理セクション
    st.subheader("📁 セッション管理")

    # セッションID手動入力
    manual_session_id = st.text_input(
        "セッションIDを指定",
        placeholder="session-id-xxxx...",
        help="任意のセッションIDを入力して承認待ちや結果を確認できます",
    )

    if manual_session_id:
        st.session_state.selected_session_id = manual_session_id
        st.success(f"選択中: {manual_session_id[:16]}...")

    # 最近のセッション一覧
    if st.session_state.sessions:
        st.write("**最近のセッション:**")
        for i, session in enumerate(reversed(st.session_state.sessions[-5:])):
            session_id = session["session_id"]
            is_selected = st.session_state.selected_session_id == session_id
            btn_label = f"{'✓ ' if is_selected else ''}{session_id[:8]}..."
            if st.button(btn_label, key=f"sidebar_session_{i}", use_container_width=True):
                st.session_state.selected_session_id = session_id
                st.rerun()

    # 選択解除
    if st.session_state.selected_session_id:
        if st.button("🔄 選択解除", use_container_width=True):
            st.session_state.selected_session_id = None
            st.rerun()

    st.divider()
    st.subheader("制約事項")
    st.markdown("""
    - 最大待機: **8時間**
    - コンテナ再起動で**状態消失**
    - 待機中: **メモリ課金あり**
    """)

    st.divider()
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
            new_session = {
                "session_id": result["session_id"],
                "prompt": prompt[:50] + "..." if len(prompt) > 50 else prompt,
                "created_at": datetime.now().isoformat(),
                "status": "started",
            }
            st.session_state.sessions.append(new_session)
            # ★ 新しく作成したセッションを自動的に選択
            st.session_state.selected_session_id = result["session_id"]
            st.session_state.active_session_id = result["session_id"]
            st.info("💡 サイドバーで選択中のセッションを確認・変更できます")
        else:
            st.warning("予期しないレスポンス:")
            st.json(result)

# ========================================
# タブ2: 承認待ち一覧
# ========================================
with tab2:
    st.header("承認待ちリクエスト")

    # セッション選択UI
    col_session1, col_session2 = st.columns([3, 1])
    with col_session1:
        # 選択中のセッションを表示
        target_session = st.session_state.get("selected_session_id") or st.session_state.get("active_session_id")
        if target_session:
            st.info(f"📋 対象セッション: `{target_session}`")
        else:
            st.warning("⚠️ セッションが選択されていません。サイドバーでセッションIDを入力するか、「タスク開始」タブでタスクを開始してください。")

    with col_session2:
        # セッション入力用のポップオーバー的なUI
        with st.expander("別のセッションを確認"):
            check_session_id = st.text_input(
                "セッションID",
                placeholder="確認したいセッションID",
                key="tab2_session_input",
            )
            if st.button("このセッションを確認", key="tab2_check_btn"):
                if check_session_id:
                    st.session_state.selected_session_id = check_session_id
                    st.rerun()

    # ★ AWSモードでは同じコンテナにアクセスするため、session_idを渡す
    if not LOCAL_MODE and not target_session:
        st.info("DynamoDB無しバージョンでは、同じコンテナにアクセスするためセッションIDが必要です。")
        pending_result = {"pending_approvals": [], "count": 0}
    else:
        # 承認待ち一覧を取得（AWSモードではsession_idを渡してルーティング）
        pending_result = invoke_agentcore(
            {"action": "list_pending"},
            session_id=target_session if not LOCAL_MODE else None
        )

    # デバッグ: レスポンスの内容を表示
    with st.expander("🔧 デバッグ: APIレスポンス", expanded=False):
        st.write(f"**対象セッション:** {target_session or '(なし)'}")
        st.write(f"**選択セッション:** {st.session_state.get('selected_session_id') or '(なし)'}")
        st.write(f"**アクティブセッション:** {st.session_state.get('active_session_id') or '(なし)'}")
        st.write(f"**型:** {type(pending_result).__name__}")
        st.json(pending_result if isinstance(pending_result, dict) else {"raw": str(pending_result)[:1000]})

    # 結果が辞書でない場合はエラー表示
    if not isinstance(pending_result, dict):
        st.error(f"予期しないレスポンス型: {type(pending_result).__name__}")
        st.code(str(pending_result)[:500])
    elif "error" in pending_result:
        st.error(f"エラー: {pending_result['error']}")
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
                            try:
                                reason = json.loads(reason)
                            except json.JSONDecodeError:
                                reason = {"raw": reason}

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
                                    },
                                    session_id=session_id,  # ★ AWSルーティング用
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
                                    },
                                    session_id=session_id,  # ★ AWSルーティング用
                                )
                            if "status" in trust_result:
                                st.success("このツールを信頼しました！")
                                # 自動で再開
                                with st.spinner("エージェントを再開中..."):
                                    invoke_agentcore(
                                        {"action": "resume", "session_id": session_id},
                                        session_id=session_id,
                                    )
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
                                    },
                                    session_id=session_id,  # ★ AWSルーティング用
                                )
                            if "status" in reject_result:
                                st.warning("拒否しました")
                                # 自動で再開（拒否結果を反映）
                                with st.spinner("エージェントを再開中..."):
                                    invoke_agentcore(
                                        {"action": "resume", "session_id": session_id},
                                        session_id=session_id,
                                    )
                                st.rerun()
    else:
        st.warning("予期しないレスポンス:")
        st.json(pending_result)

# ========================================
# タブ3: 結果確認
# ========================================
with tab3:
    st.header("タスク結果")

    # セッション選択
    col_result1, col_result2 = st.columns([2, 2])

    with col_result1:
        # 選択中セッションがあれば表示
        default_session = st.session_state.get("selected_session_id") or st.session_state.get("active_session_id") or ""
        session_id_input = st.text_input(
            "セッションID",
            value=default_session,
            placeholder="session-id-xxxx...",
            help="結果を確認したいセッションIDを入力",
        )

    with col_result2:
        # 最近のセッションからクイック選択
        if st.session_state.sessions:
            options = [""] + [s["session_id"] for s in reversed(st.session_state.sessions[-5:])]
            labels = ["選択してください"] + [
                f"{s['session_id'][:12]}... ({s.get('prompt', '')[:15]}...)"
                for s in reversed(st.session_state.sessions[-5:])
            ]
            selected_idx = st.selectbox(
                "最近のセッションから選択",
                range(len(options)),
                format_func=lambda i: labels[i],
                key="result_session_select",
            )
            if selected_idx > 0:
                session_id_input = options[selected_idx]

    # 操作ボタン
    col_btn1, col_btn2 = st.columns(2)

    with col_btn1:
        get_result_btn = st.button("🔍 結果を取得", type="primary", use_container_width=True)

    with col_btn2:
        get_status_btn = st.button("📊 ステータス確認", use_container_width=True)

    # 結果取得
    if get_result_btn and session_id_input:
        with st.spinner("結果を取得中..."):
            result = invoke_agentcore(
                {"action": "result", "session_id": session_id_input},
                session_id=session_id_input,
            )

        if "error" in result:
            st.error(f"エラー: {result['error']}")
            if "container restarted" in result.get("error", ""):
                st.warning("💡 コンテナが再起動された可能性があります。DynamoDB無しバージョンではコンテナ再起動で状態が消失します。")
        elif "message" in result:
            st.success("✅ タスク完了")
            st.write("**結果メッセージ:**")
            st.markdown(result.get("message", ""))
            with st.expander("詳細JSON"):
                st.json(result)
        elif "status" in result:
            status = result.get("status")
            if status == "completed":
                st.success(f"✅ ステータス: {status}")
            elif status == "waiting_approval":
                st.warning(f"⏳ ステータス: {status} - 承認待ち")
                st.info("💡 「承認待ち一覧」タブで承認操作を行ってください")
            elif status == "error":
                st.error(f"❌ ステータス: {status}")
            else:
                st.info(f"ステータス: {status}")
            st.json(result)
        else:
            st.warning("予期しないレスポンス:")
            st.json(result)
    elif get_result_btn:
        st.warning("セッションIDを入力してください")

    # ステータス確認
    if get_status_btn and session_id_input:
        with st.spinner("ステータスを確認中..."):
            result = invoke_agentcore(
                {"action": "status", "session_id": session_id_input},
                session_id=session_id_input,
            )

        if "error" in result:
            st.error(f"エラー: {result['error']}")
        else:
            status = result.get("status", "unknown")
            has_agent = result.get("has_agent", False)

            col_s1, col_s2 = st.columns(2)
            with col_s1:
                st.metric("ステータス", status)
            with col_s2:
                st.metric("エージェント保持", "あり" if has_agent else "なし")

            with st.expander("詳細JSON"):
                st.json(result)
    elif get_status_btn:
        st.warning("セッションIDを入力してください")

# フッター
st.divider()
st.caption("Strands Agents HITL + AgentCore Runtime Demo (No DynamoDB)")
