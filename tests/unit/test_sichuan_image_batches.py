"""Screenshot ingestion must preserve conflicting rows and source bytes, including duplicate files."""
import hashlib
import json
import shutil

import pytest
from PIL import Image, ImageDraw
from scripts.import_sichuan_image_batches import load_pages, prepare


def fact(row, **overrides):
    return {"row_no":row, "university_code":"0042","university_name":"样例大学",
            "group_code":"101","batch":"本科批B段","min_score":600,"min_rank":1200, **overrides}


def test_conflicts_do_not_become_distinct_majors_or_overwrite_each_other():
    facts = [fact(1),fact(2,min_score=601,min_rank=1132),fact(3,batch="提前批"),
             fact(4,group_code="102"),fact(5,group_code="102"),
             fact(6,group_code="103",min_rank=1235), fact(7,group_code="104",issues=["校名待审"])]
    pages = {("历史类","640-1.webp"):("x",[(j,"rows",r) for j,r in enumerate(facts,1)])}
    _, rows = prepare(pages,{("历史类",600):(1190,1201),("历史类",601):(1120,1134)})
    assert [r["decision"] for r in rows] == ["conflict","conflict","other_batch","candidate","duplicate","review","review"]
    assert rows[0]["university_code"]=="0042"
    assert "位次不在官方同分段区间" in rows[5]["reasons"]


def test_original_grid_and_aliases_are_independent_of_reader_claims(tmp_path):
    pics=tmp_path/"images"; parsed=tmp_path/"pages"; pics.mkdir(); parsed.mkdir()
    im=Image.new("RGB",(183,100),"white"); pen=ImageDraw.Draw(im)
    for x in range(1,182,30): pen.line((x,10,x,80),fill="black")
    for y in (10,45,80): pen.line((1,y,181,y),fill="black")
    f=pics/"640-70.webp"; im.save(f,lossless=True); shutil.copyfile(f,pics/"640-71.webp")
    record={"track":"物理类","image":f.name,"sha256":hashlib.sha256(f.read_bytes()).hexdigest(),
            "expected_rows":2,"rows":[fact(1),fact(2)]}
    target=parsed/"640-70.json"; target.write_text(json.dumps(record))
    pages=load_pages([parsed],{"物理类":pics})
    inventory,rows=prepare(pages,{("物理类",600):(1190,1201)})
    assert len(inventory)==2 and len(rows)==2 and inventory[1]["duplicate_of"]==f.name
    record["rows"].pop(); record["expected_rows"]=1; target.write_text(json.dumps(record))
    with pytest.raises(ValueError,match="原图的 2 行"):
        load_pages([parsed],{"物理类":pics})
    record["sha256"]="0"*64; target.write_text(json.dumps(record))
    with pytest.raises(ValueError,match="SHA256"):
        load_pages([parsed],{"物理类":pics})
