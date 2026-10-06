# 多机位同步剪辑（本地网页）

两段带**显示时间戳**的机位视频 + 一份 WAV 主音轨，在浏览器里按“素材时刻—成片时刻”
锚点对齐、以半开区间选机，输出固定 **30 fps / 48 kHz** 的 MP4。

- 后端：Python 3.11 + FastAPI + SQLite，FFmpeg 负责探测/抽帧/解码/编码
- 前端：原生 JavaScript（无框架、无构建步骤）
- 时间全程用**有理数**（`fractions.Fraction` / 分数串），先映射再取整，绝不先把
  每段时长取整后拼接
- 逐帧预览、跳转、WAV 预览与 MP4 导出走**同一个**时间映射（`app/mapping.py`）
- 未选机区间补**黑帧**，主音轨未覆盖区间补**静音**——绝不沿用上一机位末帧
- 导出绑定**启动时的素材与剪辑快照**；编辑可继续；取消与完成竞争只产生一个终态；
  失败或取消不会出现半成品 MP4

## 一、用 Docker Compose 启动（镜像内含 FFmpeg）

```bash
docker compose up --build
```

启动后浏览器打开：

- 剪辑页：<http://localhost:8000/>（会自动跳到 `/web/index.html`）
- 接口状态：<http://localhost:8000/api/state>

上传与工程数据保存在宿主机 `./data` 目录（SQLite、抽出的 PNG 帧、PCM、导出物）。
停止服务：`Ctrl+C`，或 `docker compose down`（数据保留；加 `-v` 不影响 `./data`）。

## 二、本地直接运行（不使用容器）

需要系统中存在 `ffmpeg` / `ffprobe`（`apt install ffmpeg` 或静态构建放入 PATH）：

```bash
python3 -m pip install -r requirements.txt
MC_DATA_DIR=./data uvicorn app.main:app --host 0.0.0.0 --port 8000
```

然后访问 <http://localhost:8000/>。

## 三、剪辑页操作

1. **上传素材**：机位 A、机位 B（任意常见容器，画面内必须有显示时间戳/帧号），
   主音轨仅接受 WAV。上传时后端用 FFprobe 取精确帧率（分数）、逐帧抽出 PNG，
   WAV 解码为 float32 PCM。
2. **设置成片时长与锚点**：时长自 0 开始，支持分数（如 `25/8`、`9.375`）。
   每份素材**至少两个**锚点，素材时刻与成片时刻都必须**严格递增**；段内线性，
   锚点覆盖范围之外视为无素材。
3. **选择机位**：用互不重叠的半开区间 `[开始, 结束)` 选择 A 或 B；区间之间
   （以及片头片尾未覆盖处）成片自动补黑。主音轨不看机位区间，独立锚点映射。
4. **保存剪辑**：逐帧预览/滑条跳转/输入秒数跳转均可使用；右侧显示该输出帧对应的
   成片时刻、素材时刻、来源显示帧；“刷新主音轨预览”可试听重采样后的 48 kHz WAV。
5. **导出 MP4**：完成后得到三个文件：
   - `<id>.mp4`（H.264/AAC，30 fps、48 kHz）
   - `<id>.mapping.json`：**每个输出帧**与**每个音频样本区间**到来源的对应记录
   - `<id>.snapshot.json`：该任务绑定的素材与剪辑快照

## 四、时间映射规则（预览与导出完全一致）

- 输出帧 `n` 的目标时刻取该半开帧格中心 `t = (n + 1/2)/30`，
  仅当 `t < 成片时长` 时该帧存在（帧数 = `ceil(时长×30 − 1/2)`，不四舍五入拼段）。
- 由机位半开区间找到当前时刻的机位，再由该机位锚点的分段线性映射得到素材时刻；
  来源显示帧 = `floor(素材时刻 × 源帧率)`。目标时刻恰好落在末锚点上时取该素材
  最后一个显示帧；落在覆盖范围之外或未选机 → 黑帧。
- 输出音频样本 `s` 的目标时刻为 `(s + 1/2)/48000`，由主音轨锚点反查源采样位置
  （**整数有理数运算**定位，仅线性插值权重为浮点；变速会自然改变音高）。
  覆盖范围外写零（静音）。块在锚点处切分，跨变速点不会重复或遗漏样本。
- 由于帧格与采样格各自独立按中心取整，视频与音频的物理长度可能相差不足一个
  采样/半帧，这是两种离散网格的正常差异，二者的**时刻映射**始终一致。

## 五、生成样例素材并验证对齐

样例包含刻意不同的起点、帧率与钟速：

- 机位 A：30 fps、12 s、显示从 `00:00.000`、钟速 1.0
- 机位 B：24 fps、10 s、显示从 `00:01.500`、钟速 0.8（显示 1 秒历时 1.25 秒）
- 主音轨：44.1 kHz 单声道 WAV，每个整数秒一个 1 kHz 脉冲，时长 10 s

画面上烧录了 **FRAME 帧号** 和 **DISPLAY 显示时钟**，可逐帧肉眼/程序化核对：

```bash
# 1) 生成样例到 samples/
python3 scripts/generate_samples.py --out samples
# 可选：--a-fps 30000/1001 --b-fps 25 --b-start 3.25 --b-rate 1.2 等

# 2) 端到端验证（对一个运行中的服务走真实 HTTP）
python3 scripts/verify_alignment.py --url http://localhost:8000

#   或不启动服务、在进程内跑 TestClient：
python3 scripts/verify_alignment.py --local
```

验证内容（800+ 断言）：

- 281 个输出帧逐一与“画面烧录帧号所指的源 PNG”像素一致，黑帧逐帧为全 0
  （含机位之间的洞，证明没有用上一机位末帧填洞）；
- 半开切机边界与分数推导完全一致，输出帧中心精确为 `(2n+1)/60`，无重帧/漏帧；
- 预览 WAV 与导出 MP4 的音轨均为 48 kHz，脉冲中心落在主音轨锚点推出的位置
  （容差 30 ms）；音频块样本区间无缝铺满、覆盖外静音；
- MP4 `r_frame_rate = 30/1`，逐帧与映射清单吻合（H.264 有损容差内）；
- 导出开始后立刻改剪辑，成品仍绑定开始时的快照；
- “取消与完成竞争”只产生一个终态：完成后取消不能抹掉成品，取消的任务不留 MP4
  与临时目录，失败任务可查到错误信息。

## 六、目录结构

```
app/
  rationals.py  有理数解析/帧格计数（唯一时间数值口径）
  mapping.py    锚点、分段线性映射、逐帧计划、音频样本块计划（唯一映射口径）
  renderer.py   由快照构建 Plan：PNG 帧、48 kHz PCM/WAV、映射报告
  media.py      FFprobe / FFmpeg：探测、逐帧抽 PNG、WAV 解码
  db.py         SQLite（工程单文档：素材、锚点、区间）
  state.py      状态装载、校验与不可变导出快照
  jobs.py       导出任务：后台线程、取消竞争、单一终态、原子发布
  main.py       FastAPI：上传 / 编辑 / 预览 / 导出任务 / 静态页
web/            index.html + app.js（原生 JS 剪辑页）
scripts/        generate_samples.py 生成样例；verify_alignment.py 端到端验证
Dockerfile  docker-compose.yml  requirements.txt
data/           运行时生成（建议加入 gitignore）
```

## 七、常用 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 当前工程（时长、锚点、区间、素材元数据、可导出性） |
| PUT | `/api/state` | 保存时长/锚点/区间（全量，严格校验，失败返回 400） |
| POST | `/api/upload` | 表单字段 `role=A|B|master`、`file=…` |
| GET | `/preview/frame/{n}.png` | 第 n 帧（响应头带成片/素材时刻、来源帧、补黑原因） |
| GET | `/api/frame-info?n=` | 第 n 帧的有理数映射信息 |
| GET | `/preview/audio.wav` | 当前映射的 48 kHz 预览音轨 |
| POST | `/api/export` | 以当前快照启动导出，返回任务 id |
| GET/POST | `/api/jobs/{id}`、`/api/jobs/{id}/cancel` | 状态轮询 / 请求取消 |
