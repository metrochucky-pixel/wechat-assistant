import sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from bot_navigation import labelled_rows, bind_rows, same_list_chrome

class Navigation(unittest.TestCase):
    def test_labels_bind_original_pixels_not_model_coordinates(self):
        image=Image.new('RGB',(1080,2400),'white')
        image.putpixel((320,847),(7,8,9))
        sheet,mapping=labelled_rows(image,[459,653,847])
        self.assertEqual(mapping,{'R1':459,'R2':653,'R3':847})
        self.assertEqual(sheet.getpixel((420,2*194+97)),(7,8,9))
        rows=bind_rows({'is_list':True,'rows':[{'row_id':'R3','name':'群','at_me':False,'y':682}]},mapping)
        self.assertEqual(rows[0]['y'],847);self.assertTrue(rows[0]['_anchored'])
    def test_bad_or_duplicate_labels_never_click(self):
        row={'row_id':'R1','name':'群','at_me':False}
        for entries in ([dict(row,row_id='R9')],[row,row],[dict(row,row_id=1)],[dict(row,at_me='true')]):
            self.assertEqual(bind_rows({'is_list':True,'rows':entries},{'R1':459}),[])
    def test_non_list_never_produces_click(self):
        self.assertEqual(bind_rows({'is_list':False,'rows':[{'row_id':'R1','name':'群','at_me':False}]},{'R1':459}),[])
    def test_changed_page_stops_remaining_list_clicks(self):
        a=Image.new('RGB',(1080,2400),'white');b=a.copy()
        b.putpixel((30,30),(0,0,0));self.assertTrue(same_list_chrome(a,b))
        b.putpixel((500,150),(0,0,0));self.assertFalse(same_list_chrome(a,b))

if __name__=='__main__':unittest.main()
