# claude-code-bash-write-guard

*[English README](README.md)*

Claude Code の `PreToolUse` フックです。エージェントが自分のルールファイル
(`CLAUDE.md`、`SKILL.md`、メモリのエントリなど) を **Bash や PowerShell 経由**で
書き換えようとしたら止めて、**Edit / Write ツール**を使うよう促します。

## なぜ必要か

`Edit|Write` に登録したフック (編集前の検査、編集後のチェック) は、そのツールで
変更されたファイルしか見えません。同じファイルを `sed -i`、`>` リダイレクト、
`tee`、Bash の中の Python ワンライナーで書き換えると、どのフックも動きません。
ルールファイルの変更を Edit 側のフックで確認しているなら、ヒアドキュメント
1 つで素通りされます。

このフックはその抜け道を塞ぎます。指定したファイルについてはシェル経由を
ブロックするので、ほかのフックから見える Edit / Write が唯一の入口になります。
単体でも役に立ちます。Edit ツールによるルールファイルの変更は端末に差分として
表示されますが、シェルのワンライナーではそうなりません。

## 動作

`Bash` / `PowerShell` のツール呼び出しごとに 2 つの検査を行い、どちらかに
当たればブロックして、Edit か Write を使うようエージェントに伝えます。

1. **コマンド全体の検索。** コマンド文字列から**保護対象のパス**と
   **書き込み操作**を探します。両方あればブロックします。
2. **書き込み先の検査。** コマンドをシェルのセグメントに分け (`cd`、サブシェル、
   波括弧グループを追跡)、各書き込みの*書き込み先* (リダイレクト先、`sed -i`・
   `tee` のオペランド、コピー / 移動の宛先、PowerShell の内容書き込み
   コマンドレットのパス) をセグメントの作業ディレクトリで解決し、保護対象の
   ファイルならブロックします。この検査はブロックを増やすだけで、1 の検査が
   止めたコマンドを通すことはありません。Python / Node / Perl / Ruby のプログラム
   (インラインでもヒアドキュメントでも) は、保護対象のパスと `open(..., "w")`
   などの書き込み呼び出しの組み合わせを調べます。

保護対象 (`.md` ファイルのみ。ファイル名は全体で照合するので、`NOT_CLAUDE.md` や
`CLAUDE.md.bak` は対象外。自分の構成に合わせてリストを書き換えてください):

| リスト | 既定の内容 |
| --- | --- |
| `GOVERNANCE` (パス全体) | すべての `CLAUDE.md`、すべての `SKILL.md`、`.claude/{rules,references,agents}/*.md`、`.claude/projects/<project>/memory/*.md` |
| `legacy_GOV_DIRS` (コマンド中のディレクトリ名 + 裸の `*.md`。`cd <dir> && cat >> notes.md` の形に対応) | `.claude/references`、`.claude/rules`、`.claude/agents`、任意の `memory` ディレクトリ |

書き込み操作 (プログラム用の `WRITES`、コマンド全体の検索用の `legacy_WRITES`):

| 種類 | 例 |
| --- | --- |
| シェルのリダイレクト | `> file`、`>> file` |
| その場編集 | `sed -i` (オプションの順序は問わず、`--in-place` も対象) |
| `tee` | `echo x \| tee file` |
| コピー / 移動 / install | `cp`、`mv`、`install` (`-t DIR` を含む)、`Copy-Item`、`Move-Item`: 宛先を検査 |
| Python などのインタプリタ | `open(..., "w")` などの書き込みモード、`.write_text(`、`.write_bytes(`、`truncate(` |
| PowerShell | `Set-Content`、`Add-Content`、`Out-File` (別名 `sc` / `ac` も) |

判定の前に、書き込みに見えて書き込みではない文字列を取り除きます。

- `-m` / `--message` の引用文字列 (コミットメッセージはデータ)
- `/dev/null`、`$null`、`NUL` へのリダイレクト
- 書き込み操作を含まないヒアドキュメント本文 (保護対象の名前が出てくるだけの文章)、
  およびドキュメントファイルへの出力・ヒアドキュメント
- 読み取り専用のセグメント (`grep`、`rg`、`wc`、`Get-Content`、`Select-String`、
  `Test-Path`、`git status|diff|show|log`)
- `<name@host>` 形式のアドレス (コミットのトレーラー)
- ASCII の矢印 (`->`、`=>`、`<->`) と数値比較 (`x>0`)
- `#` コメント

## 誤検知のときの抜け方

コマンド全体の検索は文字列検索なので、保護対象を**読むだけ**で別の場所へ書く
コマンドや、保護対象のパスを例として表示するだけのコマンドでも止まることが
あります。その場合は、**同じコマンドを 15 分以内にもう一度送ると通ります**。
ブロック時のメッセージがこのことをエージェントに伝え、再送は誤検知のための
ものだと伝えます。本当の書き込みは Edit ツールで行います。ブロックしたコマンドの
記録はエージェント (またはセッション) と作業ディレクトリごとに保持し、原子的に
書き込み、新しい 1000 件に切り詰めます。

回避用のフラグや環境変数は意図的に用意していません。正しく動くエージェントは
指示どおり Edit に切り替えますし、そうでないエージェントはどのみち一番手間の
少ない出口を選びます。

## 導入

Python 3 が必要です (標準ライブラリのみ)。

1. `hooks/governance-file-bash-write-guard.py` を `~/.claude/hooks/` にコピーします。
2. [`examples/settings.json.example`](examples/settings.json.example) の配線を
   `~/.claude/settings.json` に追加します。

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Bash|PowerShell",
           "hooks": [
             {
               "type": "command",
               "command": "python ~/.claude/hooks/governance-file-bash-write-guard.py"
             }
           ]
         }
       ]
     }
   }
   ```

   Windows ではコマンドの先頭に `PYTHONUTF8=1 PYTHONIOENCODING=utf-8 ` を付けてください。
3. 自己テストを実行します: `python ~/.claude/hooks/governance-file-bash-write-guard.py --selftest`
   (24 件: ブロックすべき 9 件、通すべき 15 件)。

## 設定

| 場所 | 内容 |
| --- | --- |
| フック内の `GOVERNANCE`、`legacy_GOVERNANCE`、`legacy_GOV_DIRS` | 保護するファイル (リスト同士の内容をそろえる) |
| フック内の `WRITES`、`legacy_WRITES` | 書き込みとみなす操作 |
| フック内の `SEEN_TTL_S` (既定 `900`) | 同一コマンドの再送が通る時間 (秒) |
| フック内の `MAX_ENTRIES` (既定 `1000`) | 記憶するブロック済みコマンドの件数 |
| 環境変数 `CLAUDE_HOOK_USER_LANG` (`en` / `ja`、既定 `en`) | ユーザーが端末で見る 1 行の通知の言語。エージェントが受け取る文面は常に英語 |

## 限界

- 習慣を矯正するためのガードで、セキュリティの境界ではありません。同一コマンドの
  再送は仕様として通ります。
- 検出するのは上の表の操作だけです。`git checkout`、`git apply`、
  シェル変数から組み立てたパスは検出しません。
- 検査するのは `Bash` と `PowerShell` のツール呼び出しだけです。エージェントが
  先に作ったスクリプトファイルを後で実行して書き込む場合は、スクリプトを作った
  コマンドにパスと書き込み操作の両方が含まれていたときだけ検出されます。

## ライセンス

MIT
