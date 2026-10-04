"""Bind visible list row labels to original screenshot coordinates."""
from PIL import Image, ImageDraw, ImageFont


def same_list_chrome(before, after):
    if before.size != after.size:return False
    w,h=before.size
    for box in ((.4,.055,.6,.09),(.3,.917,.52,.939)):
        rect=tuple(int(v*(w if i%2==0 else h)) for i,v in enumerate(box))
        if before.crop(rect).convert('RGB').tobytes()!=after.crop(rect).convert('RGB').tobytes():return False
    return True


def labelled_rows(image, centers, pitch=194):
    centers = sorted(set(y for y in centers if pitch//2 <= y < image.height-pitch//2))
    if not centers:
        return image, {}
    sheet = Image.new('RGB', (image.width+100, pitch*len(centers)), 'white')
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc', 44)
    except OSError:
        font = ImageFont.load_default()
    mapping = {}
    for index, y in enumerate(centers, 1):
        top = (index-1)*pitch
        sheet.paste(image.crop((0, y-pitch//2, image.width, y+pitch//2)), (100,top))
        draw.text((5,top+55), 'R'+str(index), fill='black', font=font)
        draw.line((0,top,sheet.width,top), fill='black', width=2)
        mapping['R'+str(index)] = y
    return sheet, mapping


def bind_rows(data, mapping):
    if data.get('is_list') is not True:
        return []
    rows = data.get('rows')
    if not isinstance(rows, list):
        return []
    # Contradictory names for one visual row are not a usable click target.
    ids = [r.get('row_id') for r in rows if isinstance(r, dict)]
    result = []
    for r in rows:
        if not isinstance(r, dict): continue
        row_id = r.get('row_id')
        if not isinstance(row_id,str) or row_id not in mapping or ids.count(row_id)!=1: continue
        if not isinstance(r.get('name'),str) or not r['name'].strip() or type(r.get('at_me')) is not bool: continue
        result.append(dict(name=r['name'], y=mapping[row_id], at_me=r['at_me'],
                           unread=r.get('unread') is True, _anchored=True))
    return result
