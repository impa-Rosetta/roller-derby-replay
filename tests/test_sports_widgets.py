import os
import tkinter as tk
import unittest
from sports_widgets import RoundedCard,SoftButton,transport_state
import sports_theme as T
import studio_theme as OriginalTheme


@unittest.skipUnless(os.environ.get('DISPLAY'),'Requires X11 DISPLAY')
class LightWidgetTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.geometry('600x400+0+0');self.root.lower()
    def tearDown(self):self.root.destroy()

    def test_disabled_button_does_not_invoke_and_active_color_is_retained(self):
        calls=[]
        b=SoftButton(self.root,'操作',lambda:calls.append(1));b.pack();self.root.update()
        b.config(state='disabled');b.invoke();self.assertEqual(calls,[])
        b.config(state='normal',bg=T.INFO_BLUE_D);b.invoke()
        self.assertEqual(calls,[1]);self.assertEqual(b.color,T.INFO_BLUE_D)
        self.assertEqual(b.cget('state'),'normal')

    def test_card_follows_content_and_expanded_body_stays_inside(self):
        card=RoundedCard(self.root,padding=12);card.pack(fill='x')
        tk.Label(card.body,text='赛事控制').pack();self.root.update()
        self.assertGreaterEqual(card.winfo_height(),card.body.winfo_reqheight()+24)
        self.assertLessEqual(card.body.winfo_x()+card.body.winfo_width(),card.winfo_width())
        expanded=RoundedCard(self.root,padding=12,expand=True);expanded.pack(fill='both',expand=True)
        self.root.update()
        self.assertLessEqual(expanded.body.winfo_y()+expanded.body.winfo_height(),expanded.winfo_height())

    def test_light_theme_does_not_modify_original_monitor_theme(self):
        self.assertEqual(T.BG_PANEL,'#ffffff')
        self.assertEqual(OriginalTheme.BG_PANEL,'#161b22')

    def test_transport_button_changes_between_triangle_and_two_bars(self):
        button=SoftButton(self.root,'播放',lambda:None,symbol='play')
        button.pack();self.root.update()
        self.assertEqual(len(button.find_withtag('transport-icon')),1)
        button.config(symbol='pause',text='暂停');self.root.update()
        self.assertEqual(len(button.find_withtag('transport-icon')),2)


class TransportStateTests(unittest.TestCase):
    def test_transport_icon_tracks_playback_and_pending_resume(self):
        for state in ['live','playing']:
            self.assertEqual(transport_state(state)[0],'pause')
        self.assertEqual(transport_state('paused')[0],'play')
        for state in ['loading','buffering']:
            self.assertEqual(transport_state(state,True)[0],'pause')
            self.assertEqual(transport_state(state,False)[0],'play')


if __name__=='__main__':unittest.main()
