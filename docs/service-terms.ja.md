# 想定用途とサービス利用条件

[English (canonical)](service-terms.md) | **日本語**

英語版を正本（canonical）とします。 [README](../README.ja.md)

このrepositoryは、個人・単一ユーザー・ローカルでの実験用途を想定しています。処理範囲を
制限した互換adapterのsource codeを公開するものであり、ChatGPT/Codex subscriptionを
一般用途向けのsupport済みAPI serviceへ変換するものではありません。

- ブリッジ client token を共有しないでください。また、自分のaccountを他のユーザーが利用できる状態にしないでください。
- ChatGPT/Codex account をpoolしないでください。ユーザー別credentialやquotaを配布しないでください。
- subscription由来のaccessを再配布、計測販売、再販しないでください。
- team、hosted、public、commercial、CI inference serviceとして使用しないでください。
- 共有serviceやproduction automationには、そのbackend利用がtermsで許可されたcredentialとOpenAI公式APIを使用してください。

OpenAIは、subscription利用をAPI trafficへ変換して複数ユーザーへ再提供・共有することは
非supportであり、fraud-prevention systemのflag対象になり得ると説明しています。このprojectはOpenAIとは提携しておらず、承認・推奨・サポートも受けていません。このrepositoryにおける
技術検証は、特定deploymentの利用許可を示すものではありません。適用されるproduct termsの
確認は利用者の責任です。

MIT licenseはこのprojectのsource codeだけに適用されます。OpenAI serviceへアクセスまたは再配布する権利を付与しません。

Policy参照先: [Codex利用に関する説明](https://x.com/thsottiaux/status/2090675027670978569)、
[OpenAI Terms of Use](https://openai.com/policies/row-terms-of-use/)、
[OpenAI Account Sharing Policy](https://help.openai.com/en/articles/10471989-openai-account-sharing-policy)。
