"""生成带厂家实图和真实 Qt 截图的八页可编辑图文操作说明。"""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
WORKSPACE = ROOT.parents[1]
URL = "https://hcn2yloo448w.feishu.cn/wiki/EsALwFJ3timvKGkic4ZcXth9nvg"


def table(document, headers, rows, widths) -> None:
    """固定列宽并允许中文换行，首行重复以便打印阅读。"""
    block = document.add_table(rows=1, cols=len(headers))
    block.style = "Table Grid"
    block.autofit = False
    for index, text in enumerate(headers):
        block.rows[0].cells[index].text = text
        block.rows[0].cells[index].width = Cm(widths[index])
        block.columns[index].width = Cm(widths[index])
    repeat = OxmlElement("w:tblHeader")
    block.rows[0]._tr.get_or_add_trPr().append(repeat)
    for row in rows:
        cells = block.add_row().cells
        for index, text in enumerate(row):
            cells[index].text = str(text)
            cells[index].width = Cm(widths[index])
    for row_index, row in enumerate(block.rows):
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_after = Pt(3)
                paragraph.paragraph_format.space_before = Pt(3)
                for run in paragraph.runs:
                    run.font.size = Pt(10)
                    run.bold = row_index == 0
            if row_index == 0:
                shade = OxmlElement("w:shd")
                shade.set(qn("w:fill"), "EEF2F4")
                cell._tc.get_or_add_tcPr().append(shade)
    document.add_paragraph().paragraph_format.space_after = Pt(0)


def paragraph(document, text, bold=False) -> None:
    """用完整短句组织操作步骤，标题外不堆装饰文字。"""
    run = document.add_paragraph().add_run(text)
    run.bold = bold


def picture(document, filename, caption, width=17) -> None:
    """使用已核对的实际截图，所有合成示例在图注中明确标注。"""
    paragraph = document.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run()
    run.add_picture(str(ASSETS / filename), width=Cm(width))
    drawing = run._r.xpath(".//wp:docPr")
    if drawing:
        drawing[0].set("descr", caption)
    caption_paragraph = document.add_paragraph(caption, style="Caption")
    caption_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER


def next_page(document, title) -> None:
    """每个操作主题独立一页，避免截图与对应步骤被分页拆开。"""
    document.add_page_break()
    document.add_heading(title, level=1)


def main() -> None:
    """读取本次实测报告，只写已完成证据；未完成的物理验收单独说明。"""
    hardware = json.loads((ROOT / "hardware_latest.json").read_text(encoding="utf-8"))
    soak_path = ROOT / "soak_latest.json"
    soak = json.loads(soak_path.read_text(encoding="utf-8")) if soak_path.exists() else None
    test_report = WORKSPACE / "tmp" / "g0505m-tests.xml"
    test_count = sum(int(item.get("tests", 0)) for item in ET.parse(test_report).getroot().findall("testsuite"))
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin, section.bottom_margin = Cm(1.8), Cm(1.8)
    section.left_margin, section.right_margin = Cm(1.8), Cm(1.8)
    for name in ("Normal", "Title", "Heading 1", "Heading 2", "Caption"):
        style = document.styles[name]
        style.font.name = "Microsoft YaHei"
        style.element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        style.font.color.rgb = RGBColor(0, 0, 0)
    document.styles["Normal"].font.size = Pt(10.5)
    document.styles["Normal"].paragraph_format.space_after = Pt(6)
    document.styles["Normal"].paragraph_format.line_spacing = 1.12
    document.styles["Title"].font.size = Pt(23)
    document.styles["Heading 1"].font.size = Pt(16)
    document.styles["Heading 1"].paragraph_format.space_after = Pt(10)
    document.styles["Caption"].font.size = Pt(9)
    # 作用：默认Word标题样式可能带蓝色底边框，技术手册只保留黑字与空白。
    for border in document.styles.element.xpath(".//w:pBdr"):
        border.getparent().remove(border)
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer.add_run("GT-G0505M 教具 1.0.0  |  ").font.size = Pt(9)
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    document.core_properties.title = "GT G0505M 手掌受力教具图文操作说明"
    document.core_properties.subject = "串口版压阻手掌阵列教学与操作"

    document.add_paragraph("GT G0505M 手掌受力教具\n图文操作说明", style="Title")
    paragraph(document, "版本 1.0.0  |  Windows 64位便携版  |  2026年10月11日")
    paragraph(document, "这套教具用于观察手掌上25个感应位置的受力大小和随时间的变化。程序保留厂家估算值，支持点选曲线、主动空载归零、记录和回放，适合教师演示及小组比较实验。")
    document.add_heading("资料结果与使用边界", level=1)
    table(document, ["项目", "结论"], [
        ("传感方式", "压阻式25点阵列，可观察持续受力"),
        ("数值单位", "厂家标定估算值，单位g；并非原始电阻/ADC"),
        ("通信", "USB转TTL，115200、8位、1停止位、无奇偶校验"),
        ("帧结构", "53字节，0x40帧头，左/右标识，25路大端值，和校验"),
        ("速度", "厂家串口最快20ms/帧；本机查询为50ms，约20帧/秒"),
        ("量程", "本机只读返回G0505M_5kg，即每点5kg版本"),
        ("测量限制", "重复性±8%、迟滞＜6%、漂移＜8%；不能直接宣称精密测力"),
    ], [3.3, 14.1])
    paragraph(document, "25个位置没有覆盖整只手的所有接触区域，因此采样点合计不能叫完整整手握力；未测区域不插值成真实压力。定量比较需固定夹具、载荷、手姿和接触面积。", True)
    paragraph(document, f"实物结果：修正错接线后，COMx在{hardware['duration_seconds']:.3f}秒收到{hardware['stats']['valid_frames']}个有效右手帧，校验错误{hardware['stats']['checksum_errors']}。首次检查首尾25路均为0，尚不足以证明按压响应和安装位置。")
    paragraph(document, "厂家资料来自洛阳冠拓电子科技有限公司公开配套手册。编号见2.5节，串口接线见3.6节，协议见5.1节。")
    paragraph(document, URL)

    next_page(document, "接线与装配")
    picture(document, "manufacturer-usb-ttl.png", "厂家3.6节串口版连接电脑实图，非用户当前接线照片")
    table(document, ["采集器", "USB转TTL侧", "核对事项"], [
        ("TX", "RX", "发送接收交叉连接，不能TX接TX"),
        ("RX", "TX", "此线用于只读参数查询等下行通信"),
        ("GND", "GND", "共地，确认接口标识与实际模块"),
        ("供电", "厂家规定5V方式", "核对供电路径，避免重复并联两路5V"),
    ], [3.1, 4.6, 9.7])
    paragraph(document, "1. 断电后核对引脚丝印与原厂连接方式。供电5V不等于UART逻辑电平5V；转换模块电平需与采集器兼容。图中颜色仅适用于厂家所示线束，其他线束不能据此猜线。")
    paragraph(document, "2. 按厂家步骤粘贴配套背胶并固定感应片，FPC按接口方向插紧和锁紧。转接板、接口和排线处固定，弯曲部留活动余量。")
    paragraph(document, "3. 将采集器固定在手臂外侧；逐点确认手套感应面能贴合被测物。不要拉扯排线、用尖锐物体顶压或猛烈挤压。")
    paragraph(document, "4. 首次实验用软球、海绵块或圆滑握柄。连接电脑后确认PW等指示与软件有效帧；灯亮并不能单独证明数据接通。")
    paragraph(document, "本说明使用串口版。蓝牙版的USB充电限制和内置电池规则不能与串口版供电接法混用。")

    next_page(document, "首次启动与实物连接")
    picture(document, "ui-real-hardware.png", "真实COMx接收截图，右手帧，图中数值为0；只读查询登记5kg版本与50ms周期")
    paragraph(document, "1. 完整解压便携ZIP，双击启动G0505M教具.cmd或G0505MTeaching.exe；_internal必须与EXE同目录。其他电脑无需Python，串口驱动由设备厂家或学校管理员安装。")
    paragraph(document, "2. 选择实际USB转TTL端口，点击连接。首个有效帧会自动展示其左/右手标识，之后可以手动切换。每只手分别保存零点、曲线与峰值。")
    paragraph(document, "3. 点击参数查询，只发SET=?。明确的型号回复可以登记量程；未知回复保持未确认。不会发送修改量程、周期或左右手的指令。")
    paragraph(document, "本机实物只读返回5kg每点、50ms、右手；固件V.2026-8-7M-r5，内部全局零值20g、零点死区3g。这些是厂家处理设置，桌面没有修改；很轻接触的数值需结合这些设置理解。", True)
    paragraph(document, "没有按压也应有周期帧。若接收字节为0，检查错接线、共地、供电、模块与数据线；若字节增加但无有效帧，检查115200 8N1与协议；无新帧超过1秒会标记过期并保留最后值。")

    next_page(document, "位置编号与逐点核对")
    picture(document, "manufacturer-numbering.png", "厂家2.5节真实25点编号图，原作者所有，左图与右图互为镜像", width=16)
    table(document, ["部位", "从指尖至根部的通道编号"], [
        ("拇指", "1、6、11、16"), ("食指", "2、7、12、17"),
        ("中指", "3、8、13、18"), ("无名指", "4、9、14、19"),
        ("小指", "5、10、15、20"), ("掌部", "21～25，具体位置见厂家图"),
    ], [4.0, 13.4])
    paragraph(document, "请教师按图依次轻按每个位置，确认只有对应编号出现主要变化；记录串扰、错位及镜像情况。程序左右手按设备标识展示，实物与标识不一致时先核对安装和厂家设置，软件不擅自修改标识。")
    paragraph(document, "点选圆点或25路数值表中的对应行，右侧显示该点当前值、峰值与最近20秒曲线。圆点颜色只表达实测位置的数值大小，手掌底图不表示未测区域压力。")
    paragraph(document, "峰值是该通道本次观察区间内的最大估算值，不能用最大点代表整手握力。色标上限仅影响颜色，超过上限的数字仍完整显示和保存。")

    next_page(document, "观察归零与冻结")
    picture(document, "ui-example-left.png", "实际Qt界面截图，曲线和着色输入为合成示例，非实物按压结果")
    paragraph(document, "原始值默认保持厂家输出，不进行桌面自动零点漂移修正。原始值本身已包含采集器内部的厂家标定和处理，不能称为未处理电阻或ADC数据。")
    paragraph(document, "空载归零：释放物体并确认当前手不受力，点击空载归零并确认。程序取当前有效帧作为固定零点，重新开始该手曲线和峰值。抓握期间零点不再变化。", True)
    paragraph(document, "归零后数值=厂家原始值−固定零点。负值保留，便于观察零点偏差。可在原始值和归零后切换；未设置零点、过期或离线回放时不能重新归零。")
    paragraph(document, "冻结：数值和曲线保持画面快照，采集与记录继续。解冻后显示最新数据。冻结时禁用归零和复位峰值，避免快照含义混乱。")
    paragraph(document, "复位峰值：只针对当前手，把当前值作为新起点。持续抓握时不会假装峰值从0重新起步。切换到新的数据源会清旧曲线和零点，防止示例和实物混用。")

    next_page(document, "保存记录与离线回放")
    picture(document, "ui-replay.png", "Qt回放截图，来源为合成示例记录；支持暂停、倍速与进度定位")
    table(document, ["文件", "用途"], [
        ("channels.csv", "每帧接收时间、L/R、25路原始g和25路固定零点g"),
        ("valid_frames.bin", "校验成功的53字节原始帧"),
        ("received_bytes.bin", "全部接收字节，包括坏帧、噪声与查询返回"),
        ("session.json", "来源、量程、色标、零点事件和开始结束统计"),
    ], [5.2, 12.2])
    paragraph(document, "点击记录选择目录，每次生成独立会话文件夹。结束点击停止记录，或正常关闭程序自动保存。磁盘写入失败会报错并停止记录；不能把报错后的部分文件当完整实验。")
    paragraph(document, "回放选择channels.csv，并保留同目录session.json以恢复来源和量程。可播放/暂停、0.5/1/2倍速和拖动定位。拖动后峰值与曲线从目标前最近20秒重新统计。回放不能改实验零点。")
    paragraph(document, "协议没有设备序号和设备时间戳，电脑接收时间包含串口与调度延迟。有效帧数与校验错误可统计，不能宣称精确丢帧率或精确设备采样时刻。")

    next_page(document, "课堂实验安排")
    paragraph(document, "建议按一节课组织：先用3～5分钟辨认位置，教师示范空载和轻重按压；再分组比较，最后讨论同一物体为什么多次抓握会得到不同分布。")
    table(document, ["实验", "操作", "记录与讨论"], [
        ("逐点按压", "教师依次轻按1～25号，每点重复2次", "实际位置与编号是否对应；邻点是否变化；有无镜像"),
        ("同点轻重", "固定位置和接触面积，做轻按与较重按，各3次", "当前值和峰值如何变化；重复结果是否相同"),
        ("保持5秒", "抓握软球5秒再释放；期间不归零", "保持时数值变化；释放后偏差；迟滞与漂移"),
        ("比较物体", "软球、海绵、圆滑握柄各重复3次", "接触点数量和分布如何变；手姿和材料的影响"),
    ], [3, 6.8, 7.6])
    document.add_heading("学生记录表", level=1)
    table(document, ["物体", "感应点", "三次峰值g", "保持与释放观察"], [
        ("软球", "", "", ""), ("海绵", "", "", ""), ("握柄", "", "", ""),
    ], [3.2, 3.4, 4.8, 6.0])
    paragraph(document, "教师结论应围绕具体位置和重复条件：接触材料、手姿、面积和安装贴合都会影响输出。保持受力时仍会有迟滞和漂移，不能把一次读数当精确静态测力。", True)
    paragraph(document, "不按采样点合计给学生握力排名。若要精确测某个载荷，需要固定夹具和已知载荷逐点校验；若要完整整手握力，需要针对该定义设计独立测量装置。")

    next_page(document, "验收与程序说明")
    paragraph(document, f"软件自动测试{test_count}项已通过，包含原PVDF的29项回归测试；另有真实Qt窗口在1366×768和1024×720的截图检查。厂家样例已确认53字节、校验0x8E、高字节在前。")
    if soak and soak.get("recording_audit"):
        audit = soak["recording_audit"]
        result = "连续接收检查通过" if audit["continuous_serial_pass"] else "后段计数重置、原录制停止，连续30分钟验收未完成"
        paragraph(document, f"本机长时检查：原会话{audit['recorded_duration_seconds']:.1f}秒，保存{audit['csv_frames']:,}帧，校验错误{audit['recorded_stats']['checksum_errors']}。{result}。本次全部25路为0，尚无真实按压变化证据。", True)
    else:
        paragraph(document, "30分钟连续运行检查结果保存在配套实物记录中。仅获得周期帧不能代替位置、按压、归零、拔插和定量误差验收。")
    table(document, ["检查", "方法与当前边界"], [
        ("位置与镜像", "实物逐点轻按25点，核对左/右及编号；待教师完成"),
        ("按住释放归零", "按住5秒、释放、空载归零，确认固定零点；待实测操作"),
        ("真实USB拔插", "先记录，再拔插，观察过期和同端口恢复；模拟重连已测，实物待测"),
        ("长时间运行", "原录制约27分40秒后停止；重新连续录制30分钟，不切换数据源"),
        ("定量误差", "固定夹具和已知载荷分点重复验证；本次没有宣称计量验收"),
    ], [4.0, 13.4])
    document.add_heading("源码组织与关键逻辑", level=1)
    paragraph(document, "新程序独立在grip_teaching包，保留原PVDF项目。models定义空间帧；protocol按帧头和和校验逐字节恢复同步；sources后台读串口并重连；parameters识别只读参数；processing管理每手固定零点和20秒历史。")
    paragraph(document, "recording保存CSV、原帧和会话参数；controller过滤旧连接回调并按电脑接收间隔回放；hand_view按厂家空间位置与左右镜像绘制；ui负责点选和操作；app提供正式入口。函数和非显然逻辑均有中文说明。")
    paragraph(document, "便携包内附源码ZIP。源码运行和重建需要Python3.12及requirements-dev.txt中的开发依赖；普通课堂电脑只需要完整解压的便携目录与正常串口驱动。")
    output = ROOT / "G0505M手掌受力教具图文操作说明.docx"
    document.save(output)
    print(output)


if __name__ == "__main__":
    main()


