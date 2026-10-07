# 本地多机位同步剪辑网页

一个完全本地运行的多机位同步剪辑工具：上传 **两段带显示时间戳的机位视频** 和
**一份 WAV 主音轨**，为每份素材用「素材时刻 ↔ 成片时刻」锚点做段内线性映射，
在成片时间轴上以不重叠的半开区间选择机位（未选择处补黑），主音轨独立映射，
输出固定 **30 fps / 48 kHz** 的 MP4，并给出每帧、每段音频采样到来源的对应记录。

* 后端：Python 3.11 + FastAPI，SQLite（WAL）
* 媒体：FFmpeg（解码、重采样、mux）
* 前端：原生 HTML/CSS/JavaScript，无构建步骤
* 时间：全程 `fractions.Fraction` 有理数，绝不把每段时长先取整再拼接

## 目录

```
app/            FastAPI 应用
  time.py       有理数时间（30fps、48kHz 网格、字符串分数解析）
  timeline.py   锚点段线性映射、机位半开区间、编辑校验
  media.py      ffprobe + ffmpeg 帧/采样精确解码（RGB24 / s16le@48k）
  image.py      纯 Python PNG 编码与最近邻缩放（预览）
  render.py     逐帧/逐采样反查、黑帧与静音、对应记录（预览=导出同一函数）
  db.py         SQLite：项目/素材/编辑/导出快照
  exporter.py   后台导出：临时目录 + 原子发布 + 取消/完成单终态
static/         剪辑页（index.html, app.js, style.css）
scripts/
  sample_generator.py  生成不同起点/帧率/钟速的样例素材
  check_sample.py      端到端校验（帧条码 + 音频脉冲 + 取消竞争）
data/           运行期数据（素材、导出、SQLite）
```

## 一、Docker Compose 启动（内含 FFmpeg，推荐）

```bash
docker compose up --build
```

启动后浏览器访问：

* **剪辑页： http://localhost:8000/**
* 健康检查： http://localhost:8000/healthz
* API 文档： http://localhost:8000/docs

素材与数据库持久化在命名卷 `editor-data`（容器内 `/srv/data`）。

## 二、本地直接启动

需要系统提供 `ffmpeg`/`ffprobe` 与 Python 3.11：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 打开 http://localhost:8000/
```

> 必须以单 worker 运行（默认即此）：进程内有解码缓存和单个 SQLite 连接。

## 三、使用流程

1. **上传素材**：在剪辑页依次选择两个机位视频和一个 WAV 主音轨。
2. **设置素材时钟**：对每个素材设置
   * `material0`：容器第 0 帧/采样的**显示时间戳**（可为负，如 `-1/2`）；
   * `clock`：钟速 = 素材显示秒 / 容器秒（如 `9/10` 表示该钟走得慢）。
3. **锚点**：每份素材至少两个严格递增的锚点 `(material, timeline)`，
   可填整数、小数或分数（`1/4`、`12/5`、`30000/1001`）。相邻锚点之间为
   一条有理数线性段；跨锚点允许变速（每段斜率独立）；锚点覆盖范围之外
   一律视为无素材（视频补黑、音频补静音）。
4. **成片时长**：从 0 开始，必须是整帧（1/30 秒）且为整采样（1/48000 秒）。
5. **机位区间**：用 `[start, end)` 半开区间选择机位；区间之间不得重叠；
   没被任何区间覆盖的时间补黑。
6. **预览**：逐帧上一帧/下一帧、输入帧号跳转、30 fps 播放。预览面板同时
   显示该帧的来源对应记录（来源素材、素材时刻、来源帧序号、锚点段号）。
7. **导出**：点击「开始导出 MP4」。导出在后台线程进行，**绑定点击时刻的
   素材与剪辑快照**，期间你可以继续修改当前编辑而不影响该导出；完成后页面
   提供 MP4 与 `mapping.json` 下载；「取消」与「完成」竞争时只可能出现一个
   终态，失败或取消都不会发布半成品文件。

## 四、对应记录 mapping.json

导出成功后可下载，形如：

```json
{
  "format": {"fps_num": 30, "fps_den": 1, "sample_rate": 48000,
             "width": 320, "height": 240,
             "duration": "4", "frames": 120, "samples": 192000},
  "edit_snapshot": {"edit": {...}, "assets": [...], "nonce": "..."},
  "video": [
    {"output_frame": 0, "target_time": "0", "source": "video",
     "asset_id": 2, "material_time": "1/4",
     "source_frame_index": 0, "anchor_segment": 0},
    {"output_frame": 30, "target_time": "1", "source": "black"}
  ],
  "audio": [
    {"order": 0, "output_sample_start": 0, "output_sample_end": 960,
     "output_time_start": "0", "output_time_end": "1/50",
     "source": "audio", "asset_id": 3,
     "source_sample_start": 0, "source_sample_end": 960,
     "source_time_start": "0", "source_time_end": "1/50"}
  ]
}
```

* 视频：**每个输出帧一条**，`output_frame` 恰好 0..N-1，不重不漏；
* 音频：按来源切成区间，相邻区间首尾相接、恰好铺满 `[0, samples)`，
  `silence` 区间即数字零（静音），不存在重复边缘样本。

## 五、时间映射与精度（要点）

* 输出帧 `i` 的目标时刻为 `T = i/30`（半开帧的**起点**）；输出采样 `n` 的
  目标时刻为 `T = n/48000`。统一用这一个目标时刻反查来源。
* 锚点段：对 `T ∈ [T_k, T_{k+1})`，
  `u = u_k + (u_{k+1}-u_k)(T-T_k)/(T_{k+1}-T_k)`，全程 Fraction 运算。
* 来源帧序号：`wall_t = (u - material0) / clock`，`idx = floor(wall_t * 源fps)`。
  这保证同一来源帧在它覆盖的显示时刻内被连续选中、到边界自然推进到下一帧，
  跨变速锚点不会重复或跳帧；取覆盖该素材时刻的显示帧（不是最近邻凑帧）。
* 来源采样同理：`floor(((u-material0)/clock) * 48000)`。
* 渲染器**不带帧间缓存**：每帧先置黑再填充，机位切换/素材结束不可能沿用上一
  机位末帧；音频空洞写入数字零。
* 变速只做线性重采样（视频抽帧/重复帧、音频线性），**不做音高修正**，
  因此音频会随变速改变音高。
* 段时长不单独取整：先在连续的有理数时间轴上映射，再在帧/采样网格上 floor，
  所以各段拼接处严格相邻，不会因四舍五入产生累计误差。

## 六、生成样例素材并自动验证

样例刻意制造差异以检验同步：

| 素材 | 帧率 | 显示起点 material0 | 钟速 clock |
|---|---|---|---|
| cam_a.mp4 | 30000/1001（≈29.97） | `-1/2` s | `1` |
| cam_b.mp4 | 24 | `1/4` s | `9/10` |
| master.wav | 48 kHz stereo | 0 | 1 |

* 每个视频画面顶部有 **16 位帧序号条码**（解码即可机读），中部印有
  机位、帧号、容器秒和显示秒；底部有不同颜色的色条，切换与黑场一目了然。
* WAV 在每个整数秒开头有 20 ms 的 1 kHz 脉冲。

```bash
# 1. 生成样例（输出到 samples/）
python scripts/sample_generator.py samples

# 2. 启动服务后跑端到端校验：
#    上传 -> 设时钟 -> 建剪辑 -> 预览逐帧条码校验 -> 导出 ->
#    校验 MP4 全部 120 帧条码与 mapping 完全一致、42 帧黑场确实为黑、
#    音频脉冲落在 0/1/2/3 秒、音频区间无缝铺满、取消竞争只有一个终态、
#    导出后继续编辑产出的是新快照
python scripts/check_sample.py http://localhost:8000
```

预期结尾打印 `ALL CHECKS PASSED`。

## 七、HTTP API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 素材、当前编辑、导出列表、输出规格 |
| POST | `/api/assets?kind=video\|audio` | multipart 上传 |
| PUT | `/api/assets/{id}/clock` | 设置 material0 / clock |
| PUT | `/api/edit` | 保存并校验剪辑（duration/anchors/cuts） |
| GET | `/api/preview/{frame}` | 该输出帧 PNG；`?rec=1` 返回对应记录 JSON |
| POST | `/api/export/start` | 绑定快照启动导出 |
| POST | `/api/export/{id}/cancel` | 请求取消（与完成竞争单终态） |
| GET | `/api/export/{id}` | 状态轮询 |
| GET | `/api/export/{id}/mp4` / `/mapping` | 成品与对应记录下载 |
