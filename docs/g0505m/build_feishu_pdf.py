# 作用：将公开飞书教程的已核对内容整理为可打印的中文 PDF，并保留厂家图片与安全边界。
from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle, Image, KeepTogether
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'output/pdf/GT-G0505M_手掌抓握力传感器教程.pdf'
ASSET = ROOT / 'docs/g0505m/assets'
FONT = Path(r'C:/Windows/Fonts/simhei.ttf')
if not FONT.exists():
    FONT = Path(r'C:/Windows/Fonts/msyh.ttc')
pdfmetrics.registerFont(TTFont('CN', str(FONT)))

# 作用：统一页眉页脚，使教程打印后仍能识别来源、版本和页码。
def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont('CN', 8)
    canvas.setFillColor(colors.HexColor('#64748B'))
    canvas.drawString(18*mm, 10*mm, 'GT-G0505M 手掌抓握力传感器采集器｜公开教程整理版')
    canvas.drawRightString(192*mm, 10*mm, f'{doc.page}')
    canvas.restoreState()

styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name='TitleCN', fontName='CN', fontSize=25, leading=34, textColor=colors.HexColor('#0F172A'), alignment=TA_CENTER, spaceAfter=12))
styles.add(ParagraphStyle(name='SubCN', fontName='CN', fontSize=11, leading=18, textColor=colors.HexColor('#475569'), alignment=TA_CENTER, spaceAfter=18))
styles.add(ParagraphStyle(name='H1CN', fontName='CN', fontSize=17, leading=25, textColor=colors.HexColor('#0F3B66'), spaceBefore=10, spaceAfter=8))
styles.add(ParagraphStyle(name='H2CN', fontName='CN', fontSize=12.5, leading=20, textColor=colors.HexColor('#0F3B66'), spaceBefore=8, spaceAfter=5))
styles.add(ParagraphStyle(name='BodyCN', fontName='CN', fontSize=9.7, leading=16, textColor=colors.HexColor('#1E293B'), spaceAfter=5))
styles.add(ParagraphStyle(name='SmallCN', fontName='CN', fontSize=8.2, leading=13, textColor=colors.HexColor('#475569'), spaceAfter=4))
styles.add(ParagraphStyle(name='NoteCN', fontName='CN', fontSize=9, leading=15, textColor=colors.HexColor('#7C2D12'), backColor=colors.HexColor('#FFF7ED'), borderColor=colors.HexColor('#FDBA74'), borderWidth=.5, borderPadding=7, spaceBefore=6, spaceAfter=8))

story=[]
def P(txt, style='BodyCN'): story.append(Paragraph(txt, styles[style]))
def H(txt, level=1): story.append(Paragraph(txt, styles['H1CN' if level==1 else 'H2CN']))
def img(name, width=150*mm):
    p=ASSET/name
    if p.exists():
        im=Image(str(p)); im._restrictSize(width, 95*mm); story.append(im); story.append(Spacer(1,3*mm))
def bullets(items):
    for x in items: P('• '+x)
def table(data, widths):
    t=Table([[Paragraph(str(c),styles['SmallCN']) for c in row] for row in data], colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#DBEAFE')),('TEXTCOLOR',(0,0),(-1,0),colors.HexColor('#0F3B66')),('GRID',(0,0),(-1,-1),.35,colors.HexColor('#CBD5E1')),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
    story.append(t); story.append(Spacer(1,4*mm))

# 作用：封面先说明适用对象和测量边界，避免把25点阵列误称为整手握力计。
P('GT-G0505M', 'TitleCN'); P('手掌抓握力传感器采集器教程', 'TitleCN')
P('接线｜安装｜串口通信｜上位机｜数据保存', 'SubCN')
P('公开飞书教程整理版　|　2026年10月18日', 'SubCN')
img('vendor-fpc-collector.png', 125*mm)
P('本教程依据洛阳冠拓电子科技有限公司公开资料整理，并结合项目内已核对的串口版资料编排。25个感应点用于观察空间分布和随时间的变化；未覆盖区域不插值为真实压力，25点相加也不等于完整整手握力。', 'NoteCN')
P('公开原文：<link href="https://hcn2yloo448w.feishu.cn/wiki/EsALwFJ3timvKGkic4ZcXth9nvg" color="#2563EB">飞书产品教程</link>　视频：<link href="https://www.bilibili.com/video/BV1uzCwYTEt4/" color="#2563EB">B站视频教程</link>', 'SmallCN')
story.append(PageBreak())

H('1. 产品和规格')
P('GT-G0505M 是 5×5 阵列式薄膜压力传感器采集器，传感器通过 FPC 排线连接采集器，采集器提供 UART TTL 或 BLE5.2 数据通道。厂家数据以 g（克）为单位输出估算值；它适合位置、相对轻重和抓握过程观察，不应直接改写为牛顿或帕斯卡。')
table([['项目','厂家资料'],['感应点','5×5，共25点；单点约100 mm²'],['传感器尺寸','约160×170 mm；10 Pin、1.0 mm FPC'],['可选量程','2 kg / 5 kg / 10 kg（按型号）'],['传感器供电','3.3–5 V'],['采集器供电','DC 3.7–5 V；电流小于60 mA'],['串口','115200，8N1，无校验'],['采样周期','最低20 ms（双手模式限制可能更高）'],['接口','UART TTL、BLE5.2、Micro USB（按版本）']], [42*mm, 130*mm])
H('2. 安全和接线',1)
P('先断电，再按采集器和转接板丝印核对。TX 与对端 RX 交叉，RX 与对端 TX 交叉，GND 共地。USB-TTL 的“5V”是供电标识，不自动代表 UART 逻辑电平为5V；转换器电平必须与采集器兼容。')
P('不要根据陌生线束的颜色猜线，不要同时接入两路5V，不要在锂电池供电时再把外部5V直接并入电源端。厂家手册明确提示错误短接可能损坏采集器。')
img('manufacturer-usb-ttl.png', 135*mm)
P('串口版典型连接：采集器 TX→USB-TTL RX；采集器 RX→USB-TTL TX；GND→GND；需要外部供电时只按厂家方案接入5V。', 'SmallCN')
story.append(PageBreak())

H('3. 传感器安装')
P('先在平整、清洁、无油污的表面贴背胶，再将感应片固定到手套或夹具。FPC 插入转接板时保持方向正确，压紧锁扣；排线弯曲处留出活动余量，避免拉扯和反复折叠。')
img('vendor-fpc-adapter.png', 145*mm)
img('wiring-section-top.png', 145*mm)
P('建议首次实验使用软球、海绵块或圆滑握柄。尖锐物体、猛烈挤压和局部折叠会改变膜片受力，也可能损坏感应层。采集器固定在手臂外侧，避免外壳被手掌直接顶压。')
H('4. 打开上位机',1)
bullets(['完整解压厂家软件或项目便携包，保持 EXE 与 _internal 文件夹同目录。','在设备管理器确认 USB-TTL 实际串口号；本机曾识别为 CH340/COM3，其他电脑编号可能不同。','上位机选择对应端口，波特率设为115200，打开串口后观察周期数据。','有字节但无有效帧时，检查 8N1、TX/RX 交叉和共地；接收字节为0时，优先检查供电、线序和驱动。'])
img('ui-real-hardware.png', 145*mm)
story.append(PageBreak())

H('5. 25点编号与显示')
P('传感器按厂家编号图排列为 H1–H5 与 L1–L5 的5×5网格。左右手显示可能是镜像关系，不能只看屏幕位置推断实物编号。首次使用应逐点轻按，确认主要变化落在对应通道。')
img('manufacturer-numbering-page.png', 125*mm)
P('界面颜色只表达当前采样点相对色标上限的大小；峰值是该通道观察区间内的最大估算值。手掌底图没有覆盖的区域不代表“压力为零”，而是没有传感器测量。', 'NoteCN')
H('6. 通信协议')
P('采集器周期发送53字节数据帧。帧头为 0x40，第二字节为左右手标识 0x31 或 0x32；后续25个通道按高字节在前排列，每通道2字节，单位为 g；第53字节为前52字节累加和的低8位。')
table([['字段','长度','说明'],['B1–B2','2字节','帧头与左右手标识'],['B3–B52','50字节','25路通道，每路高字节在前'],['B53','1字节','前52字节累加和低8位']], [32*mm, 22*mm, 118*mm])
P('配置指令以回车结束：SETT=20 设置周期；SETF=0/1/2 对应2/5/10 kg；SETM=L/R 设置左右手；SET=? 查询当前参数。教学软件建议先只读查询，确认型号后再登记，不要在不清楚硬件状态时发送修改指令。')
story.append(PageBreak())

H('7. 记录、导出和回放')
bullets(['记录前固定手姿、接触材料、夹具和实验时间；每次实验创建独立目录。','导出 CSV 时保留原始通道值、时间和手标识；不要把25路求和作为整手握力。','回放时保留原记录与参数文件，拖动进度后曲线和峰值应从目标前一段观察区间重新统计。','串口协议没有设备序号和设备时间戳，电脑接收时间包含调度延迟，不能据此宣称精确设备采样时刻。'])
img('ui-replay.png', 145*mm)
H('8. 推荐课堂实验')
table([['实验','操作','可讨论结论'],['逐点按压','依次轻按1–25点','编号、镜像和串扰'],['同点轻重','同一点轻按与较重按','相对变化和峰值'],['持续抓握','软球抓握5秒再释放','保持、迟滞和漂移'],['重复比较','固定手姿重复3次','安装贴合与动作差异']], [30*mm, 68*mm, 74*mm])
P('若要测定某个载荷，需要固定夹具和已知载荷逐点标定；若要测量完整整手握力，应设计覆盖完整受力面的独立测量装置。当前阵列教程的结论应围绕“哪些位置变化、变化是否重复、条件是否一致”。', 'NoteCN')
H('9. 故障排查')
bullets(['无串口：安装匹配的 CH340/USB-TTL 驱动，重新插拔并确认设备管理器端口。','0字节：检查采集器是否上电、TX/RX 是否交叉、GND 是否共地、供电是否重复或不足。','有字节无有效帧：核对115200、8N1、帧头0x40和校验；不要把 Pico 专用协议发给 GT-G0505M。','数值全为0：先确认按压位置、FPC 锁紧和手套贴合，再检查厂家量程与内部零点设置。','数据间歇中断：查看指示灯、USB线和电源，记录原始接收字节后再判断是物理断线还是坏帧。'])
P('资料来源：洛阳冠拓电子科技有限公司公开飞书产品页及配套用户手册；项目内教程和程序仅作为本地操作补充。版本日期：2026-10-18。', 'SmallCN')

OUT.parent.mkdir(parents=True, exist_ok=True)
doc=SimpleDocTemplate(str(OUT), pagesize=A4, rightMargin=18*mm, leftMargin=18*mm, topMargin=16*mm, bottomMargin=17*mm, title='GT-G0505M 手掌抓握力传感器教程', author='OpenAI')
doc.build(story, onFirstPage=footer, onLaterPages=footer)
print(OUT)
