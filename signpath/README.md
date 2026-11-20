# SignPath 免费代码签名 · 设置指南

本目录包含用 [SignPath Foundation](https://signpath.org/) 为开源项目免费签名的全部配置。签名私钥托管在 SignPath 的 HSM，你拿不到也不需要拿到私钥；每次发布由 GitHub Actions 构建、提交签名、人工批准后发布。

> 代码侧已经就绪：`.github/workflows/build-release.yml` 会在你打 `v*` tag 时自动构建并把产物送签。你只需要完成下面**网页端的配置**。

---

## 背景（先理解，避免走弯路）

1. **签名者显示为 "SignPath Foundation"，不是你或项目名。** 这是共享证书，好处是它继承了 250+ 开源项目的 SmartScreen 信誉，通常能直接通过。
2. **每次发布需要你手动批准一次**（SignPath 的审批策略），这是 OSS 免费签名的硬性要求。
3. **所有构建必须在 GitHub 托管的 runner 上跑**——所以本地打包的 exe 不能直接送签，必须走这个 workflow（已配好）。
4. **第一次申请需要有至少一个 Release。** 所以流程分两步：先发一个未签名 Release 作为申请素材 → 申请通过 → 之后打 tag 就自动出签名版。

---

## 第 0 步：发布一个未签名 Release（申请素材）

SignPath 审核会看仓库是否"已发布、有实质内容"。先打一个 tag 触发 workflow，产出一个未签名 Release：

```bash
git tag v0.1.0
git push origin v0.1.0
```

因为此时还没配置 SignPath 变量，workflow 会自动跳过签名、发布一个标注 `(unsigned)` 的 Release。这个 Release 用来满足申请条件。

---

## 第 1 步：申请 SignPath Foundation 开源签名

1. 打开 <https://signpath.io/> → 用 **GitHub 账号登录**（就是你仓库 `Arthur-Hypp` 的那个账号）。
2. 进入 **Open Source / Free Code Signing** 申请入口，填写：
   - **Repository URL**：`https://github.com/Arthur-Hypp/SpaceKnife`
   - **License**：MIT
   - **用途说明**：可参考下方模板
3. 同时开启你 GitHub 账号和 SignPath 账号的 **MFA**（SignPath 条款要求所有成员启用）。
4. 在仓库里补一份签名策略说明（[`CODE_SIGNING_POLICY.md`](CODE_SIGNING_POLICY.md) 已备好，可直接用），审核常会看这个文件。
5. 提交后等人工审核（可能几个工作日）。

**用途说明模板**（可复制）：

> SpaceKnife is a read-only Windows utility that monitors C: drive space changes
> (snapshot diffing + USN journal attribution) and reports which folders grew
> and when. It is distributed as a standalone PyInstaller onedir bundle from
> GitHub Releases. Code signing is needed so Windows SmartScreen / Smart App
> Control does not block first-time downloads. All releases are built on
> GitHub-hosted runners via .github/workflows/build-release.yml.

---

## 第 2 步：审核通过后，在 SignPath 后台配置

1. **添加可信构建系统**：Organization → Trusted Build Systems → 添加预定义的 **GitHub.com**，并关联到你的 Project。
2. **安装 SignPath GitHub App**：Settings → Applications，授权访问 `Arthur-Hypp/SpaceKnife` 仓库（用于审计日志校验）。
3. **创建 Project**：
   - Project name / slug：建议 `SpaceKnife`（slug 存进仓库变量）。
   - Repository URL：`https://github.com/Arthur-Hypp/SpaceKnife`。
   - 关联第 1 步的 GitHub.com trusted build system。
4. **创建 Artifact Configuration**：把本目录 [`artifact-configuration.xml`](artifact-configuration.xml) 的内容粘贴进去，slug 建议 `windows-exe`。
5. **创建两个 Signing Policy**：
   - `test-signing`：用测试证书，可不审批，用于联调。
   - `release-signing`：选发行证书（SignPath Foundation），**要求人工审批**，把你设为 Approver，启用 trusted-build-system 校验。
6. **创建 API Token**：给一个只有 submitter 权限的 CI 用户生成 token（不需要审批权限）。这就是后面要填的 `SIGNPATH_API_TOKEN`。

---

## 第 3 步：在 GitHub 仓库填入配置（Secrets / Variables）

进入仓库 **Settings → Secrets and variables → Actions**：

### Secrets（New repository secret）
| 名称 | 值 |
|---|---|
| `SIGNPATH_API_TOKEN` | 第 2 步生成的 API Token |

### Variables（New repository variable）
| 名称 | 值（示例） | 说明 |
|---|---|---|
| `SIGNPATH_ORGANIZATION_ID` | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` | SignPath 后台 Organization ID |
| `SIGNPATH_PROJECT_SLUG` | `SpaceKnife` | 填了它 workflow 才会启用签名 |
| `SIGNPATH_SIGNING_POLICY_SLUG` | `release-signing` | 签名策略 slug |
| `SIGNPATH_ARTIFACT_CONFIGURATION_SLUG` | `windows-exe` | 制品配置 slug |

> ⚠️ 只要 `SIGNPATH_PROJECT_SLUG` 这一项为空，workflow 就走"未签名"模式；其余变量无论填没填都不触发签名。这样你可以先发未签名 Release、后补配置，互不干扰。
>
> `GITHUB_TOKEN` 由 Actions 自动提供，无需手动添加。

---

## 第 4 步：发布签名版

1. 打一个新 tag（版本号要高于之前那个）：

   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```

2. workflow 会：构建 → 送签 → **等待你批准**。
3. 到 SignPath 后台 **Signing Requests** 里点批准（你会收到邮件通知）。
4. 批准后 workflow 继续：校验签名 → 打包 ZIP + SHA-256 → 发布带签名资产的 Release。

验证下载后的 exe：

```powershell
signtool verify /pa CDriveSentinel.exe
Get-AuthenticodeSignature .\CDriveSentinel.exe | Format-List
```

---

## 联调建议（正式发布前）

先用 `test-signing` 策略跑通整条链路：
- 临时把 `SIGNPATH_SIGNING_POLICY_SLUG` 设为 `test-signing`
- 打一个测试 tag，确认能自动出签名版
- 再改回 `release-signing` 发正式版

---

## 常见问题

- **SmartScreen 仍然警告？** 新身份的信誉需要积累，但 SignPath Foundation 是共享的成熟身份，通常能较快通过；仍需真实下载量。
- **审计日志校验失败？** 确认 SignPath GitHub App 已安装并授权该仓库，且 workflow 所有 job 都在 GitHub-hosted runner（默认是 `windows-latest`，符合）。
- **签名验证 Status 不是 Valid？** 检查 artifact configuration 的 `path="CDriveSentinel.exe"` 是否和 ZIP 内实际路径一致（onedir 打包后主 exe 在 ZIP 根）。
