import sqlite3
import os
from threading import Thread
from migration import ensure_schema, inspect_backup, import_backup, archive_text
from pathlib import Path

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput


class NotebookStore:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS notebooks (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY,
                notebook_id INTEGER NOT NULL REFERENCES notebooks(id),
                title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '');
        ''')
        if not self.notebooks():
            self.add_notebook('Il mio quaderno')

    def notebooks(self):
        return self.db.execute('SELECT id,name FROM notebooks ORDER BY id').fetchall()

    def pages(self, notebook):
        return self.db.execute(
            'SELECT id,title FROM pages WHERE notebook_id=? ORDER BY id',
            (notebook,)).fetchall()

    def page(self, page_id):
        return self.db.execute('SELECT title,body FROM pages WHERE id=?',
                               (page_id,)).fetchone()

    def add_notebook(self, name):
        with self.db:
            cursor = self.db.execute('INSERT INTO notebooks(name) VALUES (?)', (name,))
            notebook = cursor.lastrowid
            self.db.execute('INSERT INTO pages(notebook_id,title) VALUES (?,?)',
                            (notebook, 'Prima pagina'))
        return notebook

    def add_page(self, notebook):
        title = 'Pagina {}'.format(len(self.pages(notebook)) + 1)
        with self.db:
            cursor = self.db.execute('INSERT INTO pages(notebook_id,title) VALUES (?,?)',
                                     (notebook, title))
        return cursor.lastrowid

    def save(self, page_id, title, body):
        with self.db:
            self.db.execute('UPDATE pages SET title=?,body=? WHERE id=?',
                            (title, body, page_id))


class NessoApp(App):
    def build(self):
        self.title = 'Nesso'
        Window.clearcolor = (0.94, 0.96, 0.94, 1)
        Window.softinput_mode = 'below_target'
        self.loading = True
        self.page_id = None
        self.dirty = False
        self.store = NotebookStore(Path(self.user_data_dir) / 'nesso.sqlite3')
        ensure_schema(self.store.db)
        self.import_busy = False
        self.delayed_save = Clock.create_trigger(self.save, 0.7)
        root = BoxLayout(orientation='vertical', padding=dp(12), spacing=dp(8))
        root.add_widget(Label(text='NESSO  /  Quaderni', font_size='23sp',
                              color=(0.12, 0.28, 0.23, 1),
                              size_hint_y=None, height=dp(42)))

        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        self.notebooks = Spinner(text='', font_size='16sp')
        row.add_widget(self.notebooks)
        row.add_widget(self.button('+ Quaderno', self.new_notebook))
        root.add_widget(row)
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        self.pages = Spinner(text='', font_size='16sp')
        row.add_widget(self.pages)
        row.add_widget(self.button('+ Pagina', self.new_page))
        root.add_widget(row)

        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        row.add_widget(self.button('Importa ZIP', self.select_backup))
        row.add_widget(self.button('Archivio', self.show_archive))
        root.add_widget(row)
        self.page_title = TextInput(multiline=False, hint_text='Titolo della pagina',
                                    font_size='20sp', size_hint_y=None, height=dp(50),
                                    padding=[dp(12), dp(10)])
        root.add_widget(self.page_title)
        self.editor = TextInput(multiline=True, hint_text='Inizia a scrivere...',
                                font_size='19sp', padding=[dp(18), dp(18)],
                                background_normal='', background_active='',
                                background_color=(1, 1, 1, 1),
                                foreground_color=(0.12, 0.18, 0.16, 1))
        root.add_widget(self.editor)
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        self.status = Label(text='', color=(0.18, 0.35, 0.28, 1), font_size='13sp')
        row.add_widget(self.status)
        row.add_widget(self.button('Salva', self.save))
        root.add_widget(row)

        self.notebooks.bind(text=self.choose_notebook)
        self.pages.bind(text=self.choose_page)
        self.page_title.bind(text=self.changed)
        self.editor.bind(text=self.changed)
        self.load_notebook(self.store.notebooks()[0][0])
        return root

    def button(self, text, callback):
        button = Button(text=text, size_hint_x=None, width=dp(112),
                        font_size='14sp', background_normal='',
                        background_color=(0.16, 0.36, 0.29, 1))
        button.bind(on_release=callback)
        return button

    def changed(self, *args):
        if self.loading:
            return
        self.dirty = True
        self.status.text = 'Salvataggio...'
        self.delayed_save()

    def save(self, *args):
        if self.page_id is None or not self.dirty:
            return True
        self.delayed_save.cancel()
        try:
            self.store.save(self.page_id, self.page_title.text, self.editor.text)
        except sqlite3.Error:
            self.status.text = 'Errore: testo non salvato'
            return False
        self.dirty = False
        self.status.text = 'Salvato sul dispositivo'
        self.refresh_pages()
        return True

    def refresh_pages(self):
        previous = self.loading
        self.loading = True
        self.page_map = {f'{i} - {title or "Senza titolo"}': i
                         for i, title in self.store.pages(self.notebook_id)}
        self.pages.values = list(self.page_map)
        self.pages.text = next((label for label, i in self.page_map.items()
                                if i == self.page_id), '')
        self.loading = previous

    def load_notebook(self, notebook_id):
        self.loading = True
        self.notebook_id = notebook_id
        self.notebook_map = {f'{i} - {name}': i for i, name in self.store.notebooks()}
        self.notebooks.values = list(self.notebook_map)
        self.notebooks.text = next(label for label, i in self.notebook_map.items()
                                   if i == notebook_id)
        pages = self.store.pages(notebook_id)
        if not pages:
            self.page_id = None
            self.page_title.text = ''
            self.editor.text = ''
            self.page_title.disabled = True
            self.editor.disabled = True
            self.refresh_pages()
            self.loading = False
            self.status.text = 'Quaderno vuoto: premi + Pagina'
            return
        self.load_page(pages[0][0])

    def load_page(self, page_id):
        self.loading = True
        self.page_id = page_id
        self.page_title.disabled = False
        self.editor.disabled = False
        self.page_title.text, self.editor.text = self.store.page(page_id)
        self.editor.cursor = (0, 0)
        self.dirty = False
        self.refresh_pages()
        self.loading = False
        self.status.text = 'Salvato sul dispositivo'

    def choose_notebook(self, widget, label):
        if self.loading or label not in self.notebook_map:
            return
        target = self.notebook_map[label]
        if self.save():
            self.load_notebook(target)
        else:
            self.loading = True
            self.notebooks.text = next(k for k, i in self.notebook_map.items()
                                       if i == self.notebook_id)
            self.loading = False

    def choose_page(self, widget, label):
        if self.loading or label not in self.page_map:
            return
        target = self.page_map[label]
        if self.save():
            self.load_page(target)
        else:
            self.refresh_pages()

    def new_page(self, *args):
        if not self.save():
            return
        try:
            target = self.store.add_page(self.notebook_id)
        except sqlite3.Error:
            self.status.text = 'Impossibile creare la pagina'
            return
        self.load_page(target)

    def new_notebook(self, *args):
        if not self.save():
            return
        content = BoxLayout(orientation='vertical', spacing=dp(12), padding=dp(12))
        name = TextInput(multiline=False, hint_text='Nome del quaderno', font_size='18sp')
        content.add_widget(name)
        actions = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        popup = Popup(title='Nuovo quaderno', content=content,
                      size_hint=(0.9, None), height=dp(230))

        def create(*args):
            if not name.text.strip():
                name.hint_text = 'Inserisci un nome'
                return
            try:
                target = self.store.add_notebook(name.text.strip())
            except sqlite3.Error:
                popup.title = 'Errore: quaderno non creato'
                return
            popup.dismiss()
            self.load_notebook(target)

        actions.add_widget(self.button('Annulla', popup.dismiss))
        actions.add_widget(self.button('Crea', create))
        content.add_widget(actions)
        popup.open()

    def message(self, title, text):
        box = BoxLayout(orientation='vertical', spacing=dp(8), padding=dp(8))
        box.add_widget(TextInput(text=text, readonly=True, font_size='16sp'))
        popup = Popup(title=title, content=box, size_hint=(0.94, 0.85))
        box.add_widget(self.button('Chiudi', popup.dismiss))
        popup.open()

    def show_archive(self, *args):
        self.message('Mappe e cronologia (lettura)', archive_text(self.store.db))

    def select_backup(self, *args):
        if self.import_busy or not self.save():
            return
        try:
            from android import activity
            from jnius import autoclass
            from android.runnable import run_on_ui_thread
            if not getattr(self, 'picker_bound', False):
                activity.bind(on_activity_result=self.picker_result)
                self.picker_bound = True
            Intent = autoclass('android.content.Intent')
            current = autoclass('org.kivy.android.PythonActivity').mActivity
            intent = Intent(Intent.ACTION_OPEN_DOCUMENT)
            intent.setType('*/*')
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            @run_on_ui_thread
            def launch():
                current.startActivityForResult(intent, 8421)
            launch()
        except Exception as ex:
            self.message('Importazione', 'Selezione file non disponibile: ' + str(ex))

    def picker_result(self, request, result, intent):
        if request != 8421 or result != -1 or intent is None:
            return
        uri = intent.getData()
        Clock.schedule_once(lambda dt: self.begin_import(uri), 0)

    def begin_import(self, uri):
        if self.import_busy:
            return
        self.import_busy = True
        self.status.text = 'Controllo backup...'
        def read():
            try:
                from jnius import autoclass
                current = autoclass('org.kivy.android.PythonActivity').mActivity
                descriptor = current.getContentResolver().openFileDescriptor(uri, 'r')
                if descriptor is None:
                    raise ValueError('File non accessibile')
                try:
                    with os.fdopen(os.dup(descriptor.getFd()), 'rb') as stream:
                        raw = stream.read(200_000_001)
                finally:
                    descriptor.close()
                bundle = inspect_backup(raw)
                Clock.schedule_once(lambda dt: self.confirm_import(bundle), 0)
            except Exception as ex:
                error = str(ex)
                Clock.schedule_once(lambda dt: self.import_error(error), 0)
        Thread(target=read, daemon=True).start()

    def import_error(self, error):
        self.import_busy = False
        self.status.text = 'Importazione non completata'
        self.message('Backup non importato', error)

    def confirm_import(self, bundle):
        self.import_busy = False
        self.status.text = 'Backup verificato'
        box = BoxLayout(orientation='vertical', padding=dp(12), spacing=dp(10))
        box.add_widget(TextInput(readonly=True, font_size='17sp', text=bundle['summary'] +
            '\n\nI quaderni saranno aggiunti senza sostituire quelli presenti. '
            'Mappe e cronologia saranno consultabili in Archivio. '
            'Disegni, formattazione e metadati originali saranno conservati nel backup, '
            'ma non ancora riprodotti graficamente.'))
        popup = Popup(title='Importa la precedente Nesso', content=box, size_hint=(0.94, 0.7))
        actions = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        def proceed(*args):
            if not self.save():
                return
            try:
                first, added = import_backup(self.store.db, bundle)
            except Exception as ex:
                popup.dismiss()
                self.import_error(str(ex))
                return
            popup.dismiss()
            if first is not None:
                self.load_notebook(first)
            self.status.text = 'Importazione completata' if added else 'Backup gia importato'
            self.message('Importazione verificata', bundle['summary'] +
                         '\n\nIl file ZIP originale completo e conservato anche nel database.')
        actions.add_widget(self.button('Annulla', popup.dismiss))
        actions.add_widget(self.button('Importa', proceed))
        box.add_widget(actions)
        popup.open()

    def on_pause(self):
        self.save()
        return True

    def on_stop(self):
        if hasattr(self, 'store'):
            self.save()
            self.store.db.close()


if __name__ == '__main__':
    NessoApp().run()
