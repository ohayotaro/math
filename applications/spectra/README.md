# Spectra ローカル開発ネットワーク

研究成果を検査可能な疎グラフ接続へ適用する、実資金禁止の8バリデータ実験です。
研究報告は [research/REPORT.md](research/REPORT.md)、対象範囲は
[research/COVERAGE.md](research/COVERAGE.md)、設計と主張・制約は
[research/DESIGN.md](research/DESIGN.md) を参照してください。
これは **ローカル専用の研究用devnet** であり、本番・permissionlessネットワークではありません。
HTTPは `127.0.0.1` のみで待ち受け、外部チェーンやブロックチェーンAPIには接続しません。

## セットアップと一括デモ

Python 3.9以上が必要です。

```sh
cd applications/spectra
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m spectra demo
```

デモは一時ディレクトリに8個の独立したノードプロセスとSQLite台帳を作り、
新規アカウントへの送金、全ノードの高さ・状態root・残高の一致を確認してから停止します。
秘密鍵は出力しません。デモの結果には受信者の公開アドレスが含まれます。

## 手動で8プロセスを起動

1つ目の端末で初期化します。

```sh
python -m spectra init --directory "$HOME/spectra-devnet"
```

別々の8端末で、`--id` をそれぞれ `0` から `7` にして起動します。

```sh
python -m spectra node --directory "$HOME/spectra-devnet" --id 0
```

`genesis.json` にあるvalidator公開鍵を受取先として使い、さらに別の端末から送金します。
次はvalidator 1を受取先にする例です。

```sh
RECIPIENT=$(python -c 'import json, pathlib; print(json.loads(pathlib.Path.home().joinpath("spectra-devnet/genesis.json").read_text())["validators"][1]["public_key"])')
python -m spectra transfer --directory "$HOME/spectra-devnet" \
  --to "$RECIPIENT" --amount 25
```

状態確認は各ノードの `http://127.0.0.1:18400/status` から `18407` までです。
ポート範囲は `init --base-port` で変更できます。各プロセスは自身の
`data/node-N.sqlite3` と `keys/node-N.key` を使用します。

ノードを停止中に確定ブロックができた場合は、再起動後に稼働中の**直接隣接ノード**
から証明書を同期します。`topology.edges` 上の隣接先IDを指定してください。
同期は応答サイズと証明書数でページ分割し、開始時点で観測した隣接ノードの高さまで
取得して終了します。

```sh
python -m spectra sync --directory "$HOME/spectra-devnet" --id 7 --neighbor 3
```

## 信頼境界

genesisは初期残高、鍵、loopback endpoint、トポロジーを拘束し、Ed25519署名と
ブロック提案・投票の全署名はgenesis digestと用途別domain tagに結び付きます。
確定には8者中6者以上の異なる署名が必要です。固定リーダーはvalidator 0で、
view changeはありません。quorum不足時に投票ロックが残る場合があり、同じ高さの
別提案へ再投票しません。そのためリーダー停止や投票分裂では進行が停止し得ます。

秘密鍵は初期化先の `keys/` に権限 `0600` で保存します。鍵・台帳DBはGit管理外です。
devnetのディレクトリを共有・公開せず、実資金や本番鍵を決して使用しないでください。

## 開発時チェック

上記の開発依存関係をインストールし、このディレクトリから実行します。

```sh
python -m pytest tests/test_ledger.py tests/test_network.py -q
python -m pytest -q
ruff check .
mypy spectra
python -m spectra demo
python tools/inventory.py --check
python tools/evaluate_topology.py --check
```
