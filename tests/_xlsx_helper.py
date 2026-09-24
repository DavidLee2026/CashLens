"""测试用：构造最小可读的 xlsx（纯标准库，不依赖 openpyxl）。

只写到读表需要的部件；需要嵌入图时另加 cellimages 相关部件。
"""

from __future__ import annotations

import io
import zipfile


def _col(i: int) -> str:
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def make_xlsx(header: list, rows: list[list], embedded: dict[str, bytes] | None = None,
              dispimg_col: int | None = None, dispimg_ids: list[str] | None = None) -> bytes:
    """构造 xlsx。

    embedded / dispimg_col / dispimg_ids 一起用可模拟「截图列嵌入图」：
    embedded 是 {图片名: bytes}，dispimg_ids 是每行在截图列填的 DISPIMG ID。
    """
    strings: list[str] = []

    def sid(v) -> int:
        s = str(v)
        if s not in strings:
            strings.append(s)
        return strings.index(s)

    xml_rows = []
    for r_i, row in enumerate([header] + rows, 1):
        cells = []
        for c_i, v in enumerate(row):
            if v is None or v == "":
                continue
            if isinstance(v, (int, float)):
                cells.append(f'<c r="{_col(c_i)}{r_i}"><v>{v}</v></c>')
            else:
                cells.append(f'<c r="{_col(c_i)}{r_i}" t="s"><v>{sid(v)}</v></c>')
        if (dispimg_col is not None and dispimg_ids and r_i > 1
                and (r_i - 2) < len(dispimg_ids)):
            did = dispimg_ids[r_i - 2]
            if did:
                cells.append(f'<c r="{_col(dispimg_col)}{r_i}" t="s">'
                             f'<v>{sid(f"=DISPIMG(\"{did}\",1)")}</v></c>')
        xml_rows.append(f'<row r="{r_i}">' + "".join(cells) + "</row>")

    sheet = ('<?xml version="1.0"?><worksheet '
             'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             "<sheetData>" + "".join(xml_rows) + "</sheetData></worksheet>")
    sst = ('<?xml version="1.0"?><sst '
           'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           f'count="{len(strings)}" uniqueCount="{len(strings)}">'
           + "".join(f"<si><t>{_esc(s)}</t></si>" for s in strings) + "</sst>")
    wb = ('<?xml version="1.0"?><workbook '
          'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
    wbrels = ('<?xml version="1.0"?><Relationships '
              'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
              '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
              '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
              "</Relationships>")
    ct = ('<?xml version="1.0"?><Types '
          'xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Default Extension="jpeg" ContentType="image/jpeg"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
          '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
          "</Types>")
    rels = ('<?xml version="1.0"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wbrels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        z.writestr("xl/sharedStrings.xml", sst)
        if embedded:
            ci, rl, media = [], [], []
            items = list(embedded.items())
            for i, (name, blob) in enumerate(items, 1):
                rid = f"rIdImg{i}"
                # 真实 WPS 文件里 cellImage 的 name 就是 DISPIMG 的 ID（ID_xxx），
                # 不是图片文件名；这里按顺序与 dispimg_ids 配对，保持一致
                did = (dispimg_ids[i - 1] if dispimg_ids and i - 1 < len(dispimg_ids)
                       else f"ID_{i}")
                ci.append(f'<etc:cellImage name="{did}" r:embed="{rid}"/>')
                rl.append(f'<Relationship Id="{rid}" '
                          f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                          f'Target="media/{name}"/>')
                media.append((f"xl/media/{name}", blob))
            z.writestr("xl/cellimages.xml",
                       '<?xml version="1.0"?><etc:cellImages '
                       'xmlns:etc="http://www.wps.cn/officeDocument/2017/etCustomData" '
                       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                       + "".join(ci) + "</etc:cellImages>")
            z.writestr("xl/_rels/cellimages.xml.rels",
                       '<?xml version="1.0"?><Relationships '
                       'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       + "".join(rl) + "</Relationships>")
            for path, blob in media:
                z.writestr(path, blob)
    return buf.getvalue()
