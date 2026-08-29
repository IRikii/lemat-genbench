# NSCC 集群上的 Git / GitHub 配置速查

记录 2026-08-29 在 `asp2a-login-nus02` 上把 `mybench` 纳入 git 管理时做的全部配置。
下次换机器、重装、或忘了怎么 push 时看这份。

---

## TL;DR — 日常操作就三条

```bash
cd ~/lemat-genbench
git add mybench/                          # 只加你要的目录，别用 git add -A
git commit -m "feat(mybench): ..."
git push                                  # 凭据已存，不会再问任何东西
```

第一次在新机器上配置，跳到[「换台机器怎么重建」](#换台机器怎么重建)。

---

## 一、这台集群的关键约束：SSH 连不上 GitHub

**平时用的 `~/.ssh/config` 那套在这里完全无效。** 2026-08-29 实测：

| 探测 | 结果 |
|---|---|
| TCP `github.com:22` | ❌ `No route to host` —— 防火墙直接丢包 |
| TCP `ssh.github.com:443` | ✅ 能建立 TCP 连接…… |
| 但读 SSH banner | ❌ 超时无响应，`ssh -T -p 443` 两次都是 `Connection reset` |
| TCP `github.com:443` + HTTPS | ✅ `HTTP 200`，`git ls-remote` 正常 |

第 2、3 行合起来说明问题：出站 443 上有个**做协议识别的中间设备**，它接受 TCP 握手，
但一看流量不是 TLS（SSH 上来先发明文 banner）就立刻 RST。所以连 GitHub 官方推荐的
「SSH over 443」后备方案在这里也是死路。

**结论：22 被丢包 + 443 被协议识别 kill = 出站 SSH 无解。这是网络策略不是配置问题，
写多少 `~/.ssh/config` 都没用。只能走 HTTPS + Personal Access Token。**

安全性确认过：`github.com:443` 证书是 `CN=github.com`，签发者
`Sectigo Public Server Authentication CA DV E36`，`curl` 报 `ssl_verify_result=0`。
是真 GitHub，没有 TLS 中间人，token 走这条路是安全的。

### 哪天想复测网络是否放开

```bash
bash -c 'cat </dev/null >/dev/tcp/github.com/22' && echo "22 已开放"
```

若真开放了，生成密钥 + 写 `~/.ssh/config` 三分钟的事，
届时删掉下面的两条 `insteadOf` 即可切回 SSH。

---

## 二、HTTPS 的「config」写在哪 —— 与 `~/.ssh/config` 的对应关系

`~/.ssh/config` **只管 SSH 传输层，对 HTTPS 一行都不生效**。
HTTPS 有自己完全平行的一套，职责一一对应：

| 职责 | SSH 协议 | HTTPS 协议 |
|---|---|---|
| **配置文件本体** | `~/.ssh/config` | **`~/.gitconfig`**（`git config --global` 写的就是它） |
| **密钥 / 凭据（机密）** | `~/.ssh/id_ed25519` 私钥 | **`~/.git-credentials`**（存 token） |
| **服务器身份校验** | `~/.ssh/known_hosts` | 系统 CA 证书库（自动，无需配置） |
| **主机别名 / 地址改写** | `Host gh` + `HostName github.com` | `url.<base>.insteadOf` |
| **指定用哪个身份** | `IdentityFile ~/.ssh/xxx` | `credential.<url>.username` |
| **端口 / 代理** | `Port 443`、`ProxyCommand` | `http.proxy`、`http.<url>.*` |

`~/.gitconfig` 也支持按 host 分段，跟 `~/.ssh/config` 的 `Host` 块是同一个思路。
（该文件也可放在 `~/.config/git/config`，XDG 风格，二选一；
优先级：system → global(`~/.gitconfig`) → 仓库内 `.git/config`，后者覆盖前者。）

---

## 三、当前的 `~/.gitconfig`

```ini
[user]
	name = IRikii
	email = ruiqi_chen@163.com
[credential "https://github.com"]
	helper = store
	username = IRikii
[url "https://github.com/"]
	insteadOf = git@github.com:
	insteadOf = ssh://git@github.com/
```

逐段说明：

- **`[user]`** —— 只是**写进 commit 元数据的署名**，跟能不能推送无关。
  GitHub 靠它把提交关联到你的账号头像。
- **`[credential "https://github.com"]`** ——
  - `helper = store` 让 token 存进 `~/.git-credentials`，只输一次
  - `username = IRikii` 相当于 SSH 的 `User`，配了之后 push **只问 token 不问用户名**
  - 限定在 `https://github.com` 而非全局，不影响内网 GitLab 之类的其他 host
- **`[url ... insteadOf]`** —— 把 SSH 形式的 GitHub 地址自动翻译成 HTTPS。
  配好之后可以照常从 GitHub 网页复制 `git@github.com:owner/repo.git` 直接 clone，
  git 内部自动改写并复用已存 token。**这是「以后简单连 GitHub」的关键。**

⚠️ **`store` 是明文存储**。`~/.git-credentials` 权限是 `600`（仅本人可读），
但这毕竟是共享集群。缓解办法就是下面 token 那一节说的：
只授权单个仓库、只给 Contents 权限、设过期时间。

若想更谨慎，可换成内存缓存（代价是每 8 小时重输一次）：

```bash
git config --global credential."https://github.com".helper 'cache --timeout=28800'
```

---

## 四、Personal Access Token

### 生成（GitHub 网页）

Settings → Developer settings → **Personal access tokens → Fine-grained tokens**
→ Generate new token：

- **Repository access**：Only select repositories → 只勾 `IRikii/lemat-genbench`
- **Permissions** → Repository permissions → **Contents: Read and write**（推送只需这一项）
- **Expiration**：设有限期限（如 90 天），别选 No expiration

用 fine-grained 而非老式 classic token，是为了把权限限死在一个仓库、一种操作上 ——
万一泄露，别人也动不了你其他仓库。生成后的字符串（`github_pat_` 开头）**只显示一次**。

### 输入（第一次 push 时）

```
Password for 'https://IRikii@github.com':
```

- **不会问 Username**（`credential.username` 已配）
- 要的是 **token，不是 GitHub 登录密码**（GitHub 自 2021 年起不接受密码推送）
- **粘贴时屏幕什么都不显示** —— 没有星号、光标不动，这是正常的，不是卡住
- 粘贴快捷键：VS Code 集成终端 / Linux 终端 `Ctrl+Shift+V`，或右键菜单

成功后 token 落进 `~/.git-credentials`，**这是唯一一次要输**。

### Token 过期了怎么办

```bash
rm ~/.git-credentials        # 删掉旧的
git push                     # 会重新提示输入，粘新 token 即可
```

### 绝对不要做的事

**不要**把 token 拼进 remote URL（`https://user:token@github.com/...`）。
那样它会明文写进 `.git/config`，且以后任何一次 `git remote -v` 都会打到屏幕上。

---

## 五、仓库布局与「什么放哪」的原则

```
~/lemat-genbench/                    <- home，不会被清理，git 仓库在这
├── src/  tests/  scripts/           <- 上游代码，不动
├── mybench/                         <- 你的代码，本次纳入 git
│   ├── run_traj_benchmark.py
│   ├── notes/                       <- 设计存档、问题记录，随仓库走
│   └── ...
└── lemat_data -> /scratch/users/nus/ruiqiche/lemat_data    <- 软链
                  └── temp/          <- 跑批产出，走 scratch
                  └── experiments/   <- 实验数据
```

**原则：代码在 home，数据在 scratch。**

- **home**：有配额 200 T，不会被清理，适合放代码和笔记
- **scratch**：**两个月不访问会被集群清理**，只放可重跑的数据和中间产物

`lemat_data` 这个软链被 `.git/info/exclude` 排除（注意是本地 exclude，不是 `.gitignore`），
所以 **git 天然看不见 scratch 那边的任何东西** —— 往 `lemat_data/` 下丢产出，
永远不会污染 `git status`，也不需要加任何忽略规则。

`.git/info/exclude` 目前有三行：`results_final`、`lemat_data`。
它跟 `.gitignore` 的区别：**只在本地生效、不被 git 跟踪**，
所以不会跟 upstream 产生冲突，适合放这种机器专属的排除项。

### 顺带一个冷知识：`.ruff_cache` 为什么不用加忽略规则

主仓库 `.gitignore` 里**没有** `.ruff_cache` 这条。它之所以被忽略，是因为
**ruff 会在自己创建的缓存目录里塞一个 `.ruff_cache/.gitignore`，内容就一个 `*`**，
自己把自己排除掉。`git check-ignore -v .ruff_cache/` 可以验证：

```
.ruff_cache/.gitignore:2:*    .ruff_cache/
```

---

## 六、这次实际做的操作（可作为下次的模板）

```bash
# 1. 配身份 + 凭据 + URL 重写（一次性，见第三节）
git config --global user.name  "IRikii"
git config --global user.email "ruiqi_chen@163.com"
git config --global credential."https://github.com".helper store
git config --global credential."https://github.com".username IRikii
git config --global url."https://github.com/".insteadOf "git@github.com:"
git config --global --add url."https://github.com/".insteadOf "ssh://git@github.com/"
#    注意第二条 insteadOf 必须用 --add，否则会覆盖第一条

# 2. 切分支
cd ~/lemat-genbench
git switch -c mybench-mono-traj

# 3. 把代码从 scratch 搬进仓库（用 mv 不用 cp，避免两份副本各自漂移）
mv /scratch/users/nus/ruiqiche/lemat_data/mybench ~/lemat-genbench/mybench

# 4. 清缓存目录
rm -rf ~/lemat-genbench/mybench/__pycache__ ~/lemat-genbench/mybench/.ruff_cache

# 5. 把跑批产出迁去 scratch（数据不该在 home 的 git 仓库里）
mv ~/lemat-genbench/temp /scratch/users/nus/ruiqiche/lemat_data/temp

# 6. 提交前跑 lint（组织规范：ruff check → ruff format → pytest）
.venv/bin/ruff check mybench/
.venv/bin/ruff format --check mybench/

# 7. 只 add 你要的目录，检查清单后再提交
git add mybench/
git status --short
git commit -m "feat(mybench): ..."

# 8. 推送（首次会提示输 token）
git push -u origin mybench-mono-traj
```

### 两个容易踩的坑

1. **别用 `git add -A`** —— 会把 `temp/` 之类的产出目录整个卷进来。
   永远 `git add <具体目录>` 然后 `git status --short` 核对清单。
2. **两条 `insteadOf` 的第二条必须用 `--add`** ——
   `git config` 默认是覆盖同名键，不加 `--add` 会把第一条冲掉。

---

## 七、验证清单

配完之后逐条确认：

```bash
# 身份
git config user.name && git config user.email

# URL 重写生效（SSH 形式地址能走 HTTPS 返回 hash，就说明配对了）
git ls-remote git@github.com:IRikii/lemat-genbench.git HEAD

# 工作区干净
git status --short                       # 应无输出

# 提交署名是自己而非上游作者
git log -1 --format='%an <%ae>'

# 改动范围没有溢出到 src/
git diff main..<你的分支> --name-only | cut -d/ -f1 | sort -u

# 远端分支可见，且 hash 与本地 HEAD 一致
git ls-remote origin <你的分支>

# 凭据权限正确
stat -c %a ~/.git-credentials            # 应为 600
```

---

## 换台机器怎么重建

1. 跑第六节第 1 步的六条 `git config`
2. 克隆：`git clone git@github.com:IRikii/lemat-genbench.git`
   （SSH 形式地址会被自动改写成 HTTPS，可以直接从 GitHub 网页复制）
3. 第一次 push 时粘 token
4. 如果新机器也在 scratch 上放数据，记得重建软链和 `.git/info/exclude`：
   ```bash
   ln -s /scratch/users/nus/ruiqiche/lemat_data ~/lemat-genbench/lemat_data
   printf 'results_final\nlemat_data\n' >> ~/lemat-genbench/.git/info/exclude
   ```

---

## 遗留待办

`mybench/run_traj_benchmark.py:202` 的 `--output-dir` 默认值仍是 `"temp"`。
在改掉之前，**跑批时要显式带 `--output-dir lemat_data/temp`**，
否则仓库根目录会重新长出一个 `temp/`，`git status` 又脏了。

待改 4 处：`run_traj_benchmark.py` 第 202、17 行，`README.md` 第 24、40 行。
若想彻底摆脱 CWD 依赖，`run_traj_benchmark.py` 第 48-51 行已有通过
`import lemat_genbench` 反推 repo root 的现成逻辑，可复用来解析成绝对路径。
