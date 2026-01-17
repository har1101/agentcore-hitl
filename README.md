# Strands Agents HITL + AgentCore Runtime

Strands AgentsのInterrupts機能とAgentCore Runtimeの非同期実行を組み合わせたHuman in the Loopのサンプル実装。

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────────┐
│                    AgentCore Runtime                         │
│  ┌─────────────────────────────────────────────────────┐   │
│  │              BedrockAgentCoreApp                     │   │
│  │  ┌─────────────┐    ┌─────────────────────────────┐ │   │
│  │  │ /invocations│    │   Background Thread          │ │   │
│  │  │  - start    │───>│   ┌─────────────────────┐   │ │   │
│  │  │  - approve  │    │   │   Strands Agent     │   │ │   │
│  │  │  - resume   │    │   │   + ApprovalHook    │   │ │   │
│  │  │  - result   │    │   └──────────┬──────────┘   │ │   │
│  │  └─────────────┘    │              │interrupt()   │ │   │
│  │                     │              ▼              │ │   │
│  │  ┌─────────────┐    │   ┌─────────────────────┐   │ │   │
│  │  │   /ping     │    │   │ Save to DynamoDB    │   │ │   │
│  │  │ HealthyBusy │<───│   │ (pending approval)  │   │ │   │
│  │  │   Healthy   │    │   └─────────────────────┘   │ │   │
│  │  └─────────────┘    └─────────────────────────────┘ │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      DynamoDB                                │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ hitl-approvals                                       │   │
│  │  - session_id (PK)                                   │   │
│  │  - interrupt_id (SK)                                 │   │
│  │  - status (pending/approved/rejected)                │   │
│  │  - reason, response, created_at, ...                 │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

## セットアップ

### 1. DynamoDBテーブル作成

```bash
# CloudFormationでデプロイ
aws cloudformation deploy \
  --template-file dynamodb-table.yaml \
  --stack-name hitl-approvals-stack \
  --region us-west-2

# または手動で作成
aws dynamodb create-table \
  --table-name hitl-approvals \
  --attribute-definitions \
    AttributeName=session_id,AttributeType=S \
    AttributeName=interrupt_id,AttributeType=S \
    AttributeName=status,AttributeType=S \
    AttributeName=created_at,AttributeType=S \
  --key-schema \
    AttributeName=session_id,KeyType=HASH \
    AttributeName=interrupt_id,KeyType=RANGE \
  --global-secondary-indexes \
    '[{
      "IndexName": "status-index",
      "KeySchema": [
        {"AttributeName": "status", "KeyType": "HASH"},
        {"AttributeName": "created_at", "KeyType": "RANGE"}
      ],
      "Projection": {"ProjectionType": "ALL"}
    }]' \
  --billing-mode PAY_PER_REQUEST \
  --region us-west-2
```

### 2. 依存関係インストール

```bash
pip install -r requirements.txt
```

### 3. AgentCore設定

```bash
agentcore configure --entrypoint agent.py --idle-timeout 1800
```

## 使い方

### ローカルテスト

```bash
# 1. ローカル起動
agentcore launch --local

# 2. 別ターミナルでテスト
```

### API操作

#### エージェント開始
```bash
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{
    "action": "start",
    "prompt": "Please delete all temporary files in /tmp directory"
  }'
```

レスポンス例:
```json
{
  "status": "started",
  "session_id": "abc123-...",
  "task_id": 1,
  "message": "Agent task started in background..."
}
```

#### ステータス確認
```bash
curl http://localhost:8080/ping
# {"status": "HealthyBusy"} -> 処理中
# {"status": "Healthy"} -> 待機中（承認待ちまたは完了）
```

#### 承認待ち一覧
```bash
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"action": "list_pending"}'
```

レスポンス例:
```json
{
  "pending_approvals": [
    {
      "session_id": "abc123-...",
      "interrupt_id": "interrupt_xyz",
      "name": "hitl-demo-delete_files-approval",
      "reason": {
        "tool": "delete_files",
        "input": {"paths": ["/tmp/*"]},
        "message": "Tool 'delete_files' requires human approval"
      },
      "status": "pending",
      "created_at": "2024-01-15T10:30:00"
    }
  ],
  "count": 1
}
```

#### 承認
```bash
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{
    "action": "approve",
    "session_id": "abc123-...",
    "interrupt_id": "interrupt_xyz",
    "response": "y"
  }'
```

responseオプション:
- `y` - 今回のみ承認
- `t` - 信頼（以降の同ツール呼び出しは自動承認）
- `n` - 拒否

#### 拒否
```bash
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{
    "action": "reject",
    "session_id": "abc123-...",
    "interrupt_id": "interrupt_xyz",
    "reason": "Not allowed to delete files"
  }'
```

#### エージェント再開
```bash
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{
    "action": "resume",
    "session_id": "abc123-..."
  }'
```

#### 結果取得
```bash
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{
    "action": "result",
    "session_id": "abc123-..."
  }'
```

## フロー図

```
Client                  AgentCore                    DynamoDB
  │                         │                            │
  │  POST start             │                            │
  │────────────────────────>│                            │
  │  {session_id, task_id}  │                            │
  │<────────────────────────│                            │
  │                         │                            │
  │  GET /ping              │                            │
  │────────────────────────>│  (HealthyBusy)             │
  │<────────────────────────│                            │
  │                         │                            │
  │                         │  [Agent calls dangerous tool]
  │                         │  interrupt() triggered     │
  │                         │──────────────────────────->│ save_pending
  │                         │                            │
  │  GET /ping              │  (Healthy - waiting)       │
  │<────────────────────────│                            │
  │                         │                            │
  │  POST list_pending      │                            │
  │────────────────────────>│──────────────────────────->│ query
  │  [approval list]        │<───────────────────────────│
  │<────────────────────────│                            │
  │                         │                            │
  │  POST approve           │                            │
  │────────────────────────>│──────────────────────────->│ update
  │<────────────────────────│                            │
  │                         │                            │
  │  POST resume            │                            │
  │────────────────────────>│                            │
  │  {resuming}             │  [Agent continues]         │
  │<────────────────────────│                            │
  │                         │                            │
  │  GET /ping              │  (HealthyBusy)             │
  │<────────────────────────│                            │
  │                         │                            │
  │  POST result            │                            │
  │────────────────────────>│──────────────────────────->│ get_result
  │  [final result]         │<───────────────────────────│
  │<────────────────────────│                            │
```

## 環境変数

| 変数名 | デフォルト値 | 説明 |
|--------|-------------|------|
| `HITL_TABLE_NAME` | hitl-approvals | DynamoDBテーブル名 |
| `AWS_REGION` | ap-northeast-1 | AWSリージョン |

## デプロイ

```bash
# AgentCore Runtimeへデプロイ
agentcore deploy

# ステータス確認
agentcore status --verbose
```

## 危険なツール（承認が必要）

以下のツールは実行前に人間の承認が必要です:

- `delete_files` - ファイル削除
- `execute_command` - コマンド実行
- `modify_database` - データベース変更

## カスタマイズ

### 承認が必要なツールを追加

`agent.py`の`DANGEROUS_TOOLS`リストに追加:

```python
DANGEROUS_TOOLS = ["delete_files", "execute_command", "modify_database", "your_new_tool"]
```

### 承認フックのカスタマイズ

`ApprovalHook`クラスの`approve`メソッドをカスタマイズ:

```python
def approve(self, event: BeforeToolCallEvent) -> None:
    # カスタムロジックを追加
    if event.tool_use["input"].get("amount", 0) > 10000:
        # 高額な操作は追加の承認を要求
        ...
```
