# codex-openai-bridgeへの貢献

[English (canonical)](CONTRIBUTING.md) | **日本語**

bug reportと範囲を限定したpull requestを歓迎します。このprojectはpre-1.0の個人projectであり、
best-effortでmaintainします。応答、review、merge、release時期を保証しません。Issueを受け入れても、
特定の実装をmergeすると約束するものではありません。
issueとPRのdescriptionは英語または日本語で記載できます。Policy解釈では英語版を正本とします。

## コードを書く前に

1. open／closed両方のissueとpull requestを先に検索してください。
2. non-trivialな外部貢献には`scope-approved` label付きのopen issueが必要です。重複作業を避けるため、
   labelを待ち、着手前にissueへcommentしてください。このlabelは問題とscopeを承認するもので、
   特定の実装を承認するものではありません。
3. typo、broken link、明白に誤ったdocumentation exampleは直接PR可能です。ただし小さい変更に
   限ります。
4. non-trivialなPRは`Fixes #N`または`Refs #N`でissueへlinkしてください。

Maintainerは承認済みissueを独自実装したり、別のapproachを求めたり、現在のscopeに合わなくなった
作業をcloseしたりできます。着手直前とPR作成直前にissueを再確認してください。

## このprojectに適合する変更

Bridgeの変更候補は、次のすべてを満たす必要があります。

1. exactなconsumerとversionで**具体的なconsumer上の必要性**が示されている。
2. 主張する挙動を**Codex backendが実際に受理**することを示せる。決定的fixtureはcontractの証拠には
   なりますが、liveまたはoperational supportの証拠にはなりません。
3. 模倣やsilent ignoreではなく、**OpenAI互換として意味のある挙動**を維持する。
4. Bridgeが**bounded、stateless、server-owned**、loopback-only、fail-closedのままである。
5. 受理する挙動とmalformed／unsupported caseの両方に**RED → GREENのfail-closed contract**を
   作成できる。

Codexの実能力に対するstrict translation、再現可能なconsumer互換修正、validation／resource boundの
hardening、secret-negative logging修正、不正確なdocumentationの訂正は良い候補です。

次はscope外です。

- API key認証またはfallback、OpenAI Developer API routing、provider切替
- multi-account、account pooling、client-selected credential、quota配布
- public／hosted／team／commercial／resale／CI inference service
- bridge-owned OAuth、generic credential plugin、任意のexternal credential helper
- Codex backendが提供しないEmbeddings routing、vector store、Files、Batch、Realtime等の擬似実装
- Bridge内部でのshell、browser、MCP、computer-use、hosted tool実行
- 未検証fieldをsilent accept／coerce／ignoreするstrict validationの緩和

## Evidenceとsupport表記

Repositoryのevidence levelを厳密に使用してください。

- **Contract verified**: exact package versionが決定的wire shapeを生成または受理する。provider挙動の
  証明ではありません。
- **Live verified**: exact packageがreview済みbridgeと実Codex backendを通して代表操作を完了する。
- **Operationally verified**: 実applicationを実dataまたは実sourceで反復利用し、関連するmulti-turnと
  failure-recovery境界まで観測する。

Support表記にはexact version、scope、設定、未検証境界を含めてください。Live claimはowner-localの
opt-in checkであり、GitHub ActionsへCodex OAuth credentialを入れて実行しません。Contributorは
sanitized live evidenceを提示できますが、maintainerはlive／operational claimをmergeする前に独立した
owner-local実行を要求できます。

## Sensitive dataとsecurity

Issue、PR、log、screenshot、fixture、commit、review promptへcredential、access token、account ID、実prompt、
response、tool argument、encrypted reasoning data、raw upstream response、bridge client tokenを含めないで
ください。Synthetic valueを使い、bounded structure、status、error code、count、versionだけを報告します。
server-only continuation署名keyも禁止対象で、clientへ公開してはいけません。

security vulnerabilityはprivateに報告し、[SECURITY.md](SECURITY.md)の手順に従ってください。Exploitの
詳細をpublic issueやPRへ記載しないでください。

## 開発workflow

Python 3.12とlocked environmentを使用します。

```bash
uv sync --locked --all-groups
```

すべてのbehavior changeで次を行います。

1. 最小のbehavior-level testを追加
2. 実行し、期待したRED failureを記録
3. 最小変更を実装
4. focused testをGREENへする
5. full offline gateを実行

```bash
uv run pytest -q
uv run mypy src tests scripts
uv run ruff check .
uv run ruff format --check .
uv run python -m compileall -q src tests scripts
uv run python scripts/verify_systemd_unit.py
systemd-analyze --user verify deploy/systemd/codex-openai-bridge.service
git diff --check
```

これらは**baseline local gate**であり、GitHub CI matrix全体ではありません。GitHub CIでは追加で、
version-isolatedな**OpenAI SDK 3.1.0 contract**と、`.github/workflows/ci.yml`で固定したconsumer
contractを実行します。これには**LangChain 1.5.1 consumer contract**、OpenAI Agents SDK、AutoGen、
Aider、Cline CLI、Continue coreが含まれます。Matrixの正本はworkflow fileです。変更の影響を受ける
isolated laneを実行し、未実行laneを正確に記載してください。提出後はCI matrix全体へのpassが必要です。

Live testはoptionalかつcredentialを使います。READMEのopt-in手順に従い、proofへcredential valueを
貼らないでください。CIへlive credentialを追加しないでください。

## Pull request要件

Merge対象のPRは次を満たす必要があります。

- 1つの承認済みissueにfocusする。typo、broken link、明白に誤ったexampleだけに限定した
  **小さなdocumentation-only例外では承認済みissueを省略できます**
- public contractとfailure boundaryを説明する
- RED failureとexactなGREEN command／resultを含める
- parser、filesystem、process、protocol変更にはmalformed／unsupported caseを含める
- behaviorまたはsupport表記を変更する場合、英語正本と日本語訳を更新する
- support claimを提示したevidence以下に保つ
- generated cache、local path、credential、無関係なrefactorを含めない
- baseline local gateと該当するversion-isolated consumer laneにpassし、提出後のGitHub CI matrix
  全体にもpassする

Reviewで変更を求めたり、提案を受け入れない場合があります。複数commitを維持する明確な理由がない
外部貢献はsquash mergeを基本とします。

## AI-assisted contribution

AI-assisted contributionを許可します。Human contributorがdiff全体、security boundary、license、test evidenceに
責任を持ちます。提出前にgenerated codeをreviewしてください。実際に実行したcheckだけを報告し、
substantialなAI assistanceをPRで開示してください。Agentにcredentialやprivate production dataを読ませたり
投稿させたりしないでください。

## 行動とlicense

敬意を持ち、技術的かつ簡潔にやり取りしてください。Harassment、personal attack、private dataの開示は
受け入れません。Contributionにはrepositoryの[MIT License](LICENSE)が適用されます。
