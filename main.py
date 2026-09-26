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
from kivy.uix.scrollview import ScrollView
from kivy.uix.gridlayout import GridLayout
from kivy.uix.widget import Widget


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
        self.ensure_page_order()
        self.ensure_page_features()
        if not self.notebooks():
            self.add_notebook('Il mio quaderno')

    def ensure_page_order(self):
        columns = {
            row[1]
            for row in self.db.execute(
                'PRAGMA table_info(pages)'
            ).fetchall()
        }

        if 'sort_order' not in columns:
            with self.db:
                self.db.execute(
                    'ALTER TABLE pages ADD COLUMN sort_order INTEGER'
                )

        notebooks = self.db.execute(
            'SELECT id FROM notebooks ORDER BY id'
        ).fetchall()

        with self.db:
            for (notebook_id,) in notebooks:
                pages = self.db.execute(
                    '''SELECT id FROM pages
                       WHERE notebook_id=?
                       ORDER BY
                           CASE WHEN sort_order IS NULL THEN 1 ELSE 0 END,
                           sort_order,
                           id''',
                    (notebook_id,)
                ).fetchall()

                for position, (page_id,) in enumerate(pages, 1):
                    self.db.execute(
                        'UPDATE pages SET sort_order=? WHERE id=?',
                        (position, page_id)
                    )

    def ensure_page_features(self):
        columns = {
            row[1]
            for row in self.db.execute(
                'PRAGMA table_info(pages)'
            ).fetchall()
        }

        additions = (
            ('parent_id', 'INTEGER'),
            ('favorite', 'INTEGER NOT NULL DEFAULT 0'),
            ('trashed', 'INTEGER NOT NULL DEFAULT 0'),
            ('updated_at', 'TEXT')
        )

        with self.db:
            for name, definition in additions:
                if name not in columns:
                    self.db.execute(
                        'ALTER TABLE pages ADD COLUMN '
                        + name + ' ' + definition
                    )

            self.db.execute(
                '''UPDATE pages
                   SET updated_at=CURRENT_TIMESTAMP
                   WHERE updated_at IS NULL'''
            )

    def favorite_page(self, page_id):
        with self.db:
            self.db.execute(
                '''UPDATE pages
                   SET favorite=CASE favorite WHEN 0 THEN 1 ELSE 0 END,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=?''',
                (page_id,)
            )

    def page_is_favorite(self, page_id):
        row = self.db.execute(
            'SELECT favorite FROM pages WHERE id=?',
            (page_id,)
        ).fetchone()
        return bool(row and row[0])

    def trash_page(self, page_id):
        row = self.db.execute(
            'SELECT notebook_id FROM pages WHERE id=?',
            (page_id,)
        ).fetchone()

        if row is None:
            return None

        with self.db:
            self.db.execute(
                '''UPDATE pages
                   SET trashed=1,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=?''',
                (page_id,)
            )

        return row[0]

    def restore_page(self, page_id):
        with self.db:
            self.db.execute(
                '''UPDATE pages
                   SET trashed=0,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=?''',
                (page_id,)
            )

    def trashed_pages(self):
        return self.db.execute(
            '''SELECT pages.id,pages.title,notebooks.name
               FROM pages
               JOIN notebooks
                 ON notebooks.id=pages.notebook_id
               WHERE pages.trashed=1
               ORDER BY pages.updated_at DESC,pages.id DESC'''
        ).fetchall()

    def favorite_pages(self):
        return self.db.execute(
            '''SELECT pages.id,pages.title,notebooks.name
               FROM pages
               JOIN notebooks
                 ON notebooks.id=pages.notebook_id
               WHERE pages.favorite=1
                 AND pages.trashed=0
               ORDER BY pages.updated_at DESC,pages.id DESC'''
        ).fetchall()

    def recent_pages(self, limit=20):
        return self.db.execute(
            '''SELECT pages.id,pages.title,notebooks.name
               FROM pages
               JOIN notebooks
                 ON notebooks.id=pages.notebook_id
               WHERE pages.trashed=0
               ORDER BY pages.updated_at DESC,pages.id DESC
               LIMIT ?''',
            (limit,)
        ).fetchall()

    def set_parent_page(self, page_id, parent_id):
        if parent_id == page_id:
            raise ValueError(
                'Una pagina non può essere sottopagina di se stessa'
            )

        if parent_id is not None:
            child = self.db.execute(
                'SELECT notebook_id FROM pages WHERE id=?',
                (page_id,)
            ).fetchone()
            parent = self.db.execute(
                'SELECT notebook_id FROM pages WHERE id=?',
                (parent_id,)
            ).fetchone()

            if child is None or parent is None:
                raise ValueError('Pagina non trovata')

            if child[0] != parent[0]:
                raise ValueError(
                    'La pagina principale deve essere nello stesso quaderno'
                )

        with self.db:
            self.db.execute(
                '''UPDATE pages
                   SET parent_id=?,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=?''',
                (parent_id, page_id)
            )

    def next_page_order(self, notebook_id):
        row = self.db.execute(
            '''SELECT COALESCE(MAX(sort_order), 0)
               FROM pages
               WHERE notebook_id=?''',
            (notebook_id,)
        ).fetchone()
        return (row[0] or 0) + 1

    def notebooks(self):
        return self.db.execute('SELECT id,name FROM notebooks ORDER BY id').fetchall()

    def pages(self, notebook):
        return self.db.execute(
            '''SELECT id,title FROM pages
               WHERE notebook_id=?
                 AND trashed=0
               ORDER BY sort_order, id''',
            (notebook,)).fetchall()

    def page(self, page_id):
        return self.db.execute('SELECT title,body FROM pages WHERE id=?',
                               (page_id,)).fetchone()

    def add_notebook(self, name):
        with self.db:
            cursor = self.db.execute('INSERT INTO notebooks(name) VALUES (?)', (name,))
            notebook = cursor.lastrowid
            self.db.execute(
                '''INSERT INTO pages(
                       notebook_id,title,sort_order
                   ) VALUES (?,?,?)''',
                (notebook, 'Prima pagina', 1)
            )
        return notebook

    def add_page(self, notebook):
        title = 'Pagina {}'.format(len(self.pages(notebook)) + 1)
        with self.db:
            cursor = self.db.execute(
                '''INSERT INTO pages(
                       notebook_id,title,sort_order
                   ) VALUES (?,?,?)''',
                (
                    notebook,
                    title,
                    self.next_page_order(notebook)
                )
            )
        return cursor.lastrowid

    def save(self, page_id, title, body):
        with self.db:
            self.db.execute(
                '''UPDATE pages
                   SET title=?,body=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=?''',
                (title, body, page_id)
            )

    def rename_notebook(self, notebook_id, name):
        name = name.strip()
        if not name:
            raise ValueError('Il nome del quaderno non può essere vuoto')
        with self.db:
            self.db.execute(
                'UPDATE notebooks SET name=? WHERE id=?',
                (name, notebook_id)
            )

    def delete_notebook(self, notebook_id):
        notebooks = self.notebooks()

        if len(notebooks) <= 1:
            raise ValueError(
                'Non puoi eliminare l\'ultimo quaderno'
            )

        exists = any(
            item_id == notebook_id
            for item_id, _ in notebooks
        )
        if not exists:
            raise ValueError('Quaderno non trovato')

        with self.db:
            self.db.execute(
                'DELETE FROM pages WHERE notebook_id=?',
                (notebook_id,)
            )
            self.db.execute(
                'DELETE FROM notebooks WHERE id=?',
                (notebook_id,)
            )

    def rename_page(self, page_id, title):
        title = title.strip() or 'Senza titolo'
        with self.db:
            self.db.execute(
                'UPDATE pages SET title=? WHERE id=?',
                (title, page_id)
            )

    def duplicate_page(self, page_id):
        row = self.db.execute(
            'SELECT notebook_id,title,body FROM pages WHERE id=?',
            (page_id,)
        ).fetchone()
        if row is None:
            raise ValueError('Pagina non trovata')
        notebook_id, title, body = row
        with self.db:
            cursor = self.db.execute(
                '''INSERT INTO pages(
                       notebook_id,title,body,sort_order
                   ) VALUES (?,?,?,?)''',
                (
                    notebook_id,
                    (title or 'Senza titolo') + ' - copia',
                    body,
                    self.next_page_order(notebook_id)
                )
            )
        return cursor.lastrowid

    def delete_page(self, page_id):
        row = self.db.execute(
            'SELECT notebook_id FROM pages WHERE id=?',
            (page_id,)
        ).fetchone()
        if row is None:
            return None
        notebook_id = row[0]
        with self.db:
            self.db.execute('DELETE FROM pages WHERE id=?', (page_id,))
        return notebook_id

    def move_page(self, page_id, target_notebook_id):
        row = self.db.execute(
            'SELECT notebook_id FROM pages WHERE id=?',
            (page_id,)
        ).fetchone()

        if row is None:
            raise ValueError('Pagina non trovata')

        old_notebook_id = row[0]

        if old_notebook_id == target_notebook_id:
            return

        target_exists = self.db.execute(
            'SELECT 1 FROM notebooks WHERE id=?',
            (target_notebook_id,)
        ).fetchone()

        if target_exists is None:
            raise ValueError('Quaderno di destinazione non trovato')

        new_order = self.next_page_order(target_notebook_id)

        with self.db:
            self.db.execute(
                '''UPDATE pages
                   SET notebook_id=?, sort_order=?
                   WHERE id=?''',
                (target_notebook_id, new_order, page_id)
            )
            self.normalize_page_order(old_notebook_id)
            self.normalize_page_order(target_notebook_id)

    def normalize_page_order(self, notebook_id):
        rows = self.db.execute(
            '''SELECT id FROM pages
               WHERE notebook_id=?
               ORDER BY sort_order, id''',
            (notebook_id,)
        ).fetchall()

        for position, (page_id,) in enumerate(rows, 1):
            self.db.execute(
                'UPDATE pages SET sort_order=? WHERE id=?',
                (position, page_id)
            )

    def move_page_by(self, page_id, delta):
        row = self.db.execute(
            'SELECT notebook_id FROM pages WHERE id=?',
            (page_id,)
        ).fetchone()

        if row is None:
            return False

        notebook_id = row[0]
        rows = self.db.execute(
            '''SELECT id FROM pages
               WHERE notebook_id=?
               ORDER BY sort_order, id''',
            (notebook_id,)
        ).fetchall()

        ids = [item[0] for item in rows]

        try:
            index = ids.index(page_id)
        except ValueError:
            return False

        target = index + delta

        if target < 0 or target >= len(ids):
            return False

        ids[index], ids[target] = ids[target], ids[index]

        with self.db:
            for position, current_id in enumerate(ids, 1):
                self.db.execute(
                    'UPDATE pages SET sort_order=? WHERE id=?',
                    (position, current_id)
                )

        return True

    def search_pages(self, query, notebook_id=None):
        query = query.strip()
        if not query:
            return []
        pattern = '%' + query + '%'
        if notebook_id is None:
            return self.db.execute(
                '''SELECT pages.id, pages.title, notebooks.name
                   FROM pages
                   JOIN notebooks ON notebooks.id=pages.notebook_id
                   WHERE pages.title LIKE ? OR pages.body LIKE ?
                   ORDER BY pages.id DESC''',
                (pattern, pattern)
            ).fetchall()
        return self.db.execute(
            '''SELECT pages.id, pages.title, notebooks.name
               FROM pages
               JOIN notebooks ON notebooks.id=pages.notebook_id
               WHERE pages.notebook_id=?
                 AND (pages.title LIKE ? OR pages.body LIKE ?)
               ORDER BY pages.id DESC''',
            (notebook_id, pattern, pattern)
        ).fetchall()

    def page_position(self, page_id):
        row = self.db.execute(
            'SELECT notebook_id FROM pages WHERE id=?',
            (page_id,)
        ).fetchone()
        if row is None:
            return 0, 0
        ids = [
            item[0] for item in self.db.execute(
                '''SELECT id FROM pages
                   WHERE notebook_id=?
                   ORDER BY sort_order, id''',
                (row[0],)
            ).fetchall()
        ]
        try:
            return ids.index(page_id) + 1, len(ids)
        except ValueError:
            return 0, len(ids)


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
        self.notebooks = Spinner(text='Quaderno')
        row.add_widget(self.notebooks)
        row.add_widget(self.button('+ Quaderno', self.new_notebook))
        row.add_widget(
            self.button('Rinomina', self.rename_current_notebook)
        )
        row.add_widget(
            self.button('Elimina', self.confirm_delete_current_notebook)
        )
        # NESSO 3.5C - navigazione legacy nascosta
        self.legacy_notebook_row = row

        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        self.pages = Spinner(text='', font_size='16sp')
        row.add_widget(self.pages)
        row.add_widget(self.button('+ Pagina', self.new_page))
        self.legacy_page_row = row

        page_actions_scroll = ScrollView(
            size_hint_y=None,
            height=dp(46),
            do_scroll_y=False,
            bar_width=0
        )
        page_actions = BoxLayout(
            size_hint_x=None,
            height=dp(46),
            spacing=dp(6)
        )
        page_actions.bind(
            minimum_width=page_actions.setter('width')
        )

        previous_button = self.button('←', self.previous_page)
        previous_button.width = dp(58)

        next_button = self.button('→', self.next_page)
        next_button.width = dp(58)

        search_button = self.button(
            'Cerca',
            self.search_current_notebook
        )

        duplicate_button = self.button(
            'Duplica',
            self.duplicate_current_page
        )

        rename_button = self.button(
            'Rinomina',
            self.rename_current_page
        )

        delete_button = self.button(
            'Elimina',
            self.confirm_delete_current_page
        )

        page_actions.add_widget(previous_button)
        page_actions.add_widget(next_button)
        page_actions.add_widget(search_button)
        page_actions.add_widget(duplicate_button)

        page_actions.add_widget(
            self.button('↑', self.move_page_up)
        )
        page_actions.add_widget(
            self.button('↓', self.move_page_down)
        )
        page_actions.add_widget(
            self.button('Sposta', self.move_page_to_notebook)
        )
        page_actions.add_widget(
            self.button('Sottopagina', self.set_current_subpage)
        )
        page_actions.add_widget(
            self.button('★', self.toggle_current_favorite)
        )
        page_actions.add_widget(
            self.button('Preferiti', self.show_favorite_pages)
        )
        page_actions.add_widget(
            self.button('Recenti', self.show_recent_pages)
        )
        page_actions.add_widget(
            self.button('Cestino', self.show_trash)
        )

        page_actions.add_widget(rename_button)
        page_actions.add_widget(delete_button)

        page_actions_scroll.add_widget(page_actions)
        self.legacy_page_actions = page_actions_scroll

        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        row.add_widget(self.button('Importa ZIP', self.select_backup))
        row.add_widget(self.button('Archivio', self.show_archive))
        self.legacy_archive_row = row
        # ---------------------------------------------------------
        # NESSO 3.5 - Workspace tablet
        # ---------------------------------------------------------
        self.workspace = BoxLayout(
            orientation='horizontal',
            spacing=dp(10)
        )

        self.sidebar = BoxLayout(
            orientation='vertical',
            size_hint_x=None,
            width=dp(245),
            spacing=dp(7),
            padding=[dp(8), dp(8)]
        )

        self.sidebar.add_widget(
            Label(
                text='QUADERNO',
                font_size='15sp',
                color=(0.12, 0.28, 0.23, 1),
                size_hint_y=None,
                height=dp(34)
            )
        )

        self.sidebar_notebook_title = Label(
            text='',
            font_size='18sp',
            color=(0.12, 0.28, 0.23, 1),
            size_hint_y=None,
            height=dp(42),
            halign='left',
            valign='middle'
        )
        self.sidebar_notebook_title.bind(
            size=lambda obj, value: setattr(
                obj, 'text_size', value
            )
        )
        self.sidebar.add_widget(self.sidebar_notebook_title)

        sidebar_actions = BoxLayout(
            size_hint_y=None,
            height=dp(42),
            spacing=dp(5)
        )
        sidebar_actions.add_widget(
            self.button('+ Pagina', self.new_page)
        )
        sidebar_actions.add_widget(
            self.button('Cerca', self.search_current_notebook)
        )
        self.sidebar.add_widget(sidebar_actions)

        self.sidebar_scroll = ScrollView(
            do_scroll_x=False,
            bar_width=dp(4)
        )

        self.sidebar_pages = GridLayout(
            cols=1,
            spacing=dp(4),
            size_hint_y=None
        )
        self.sidebar_pages.bind(
            minimum_height=self.sidebar_pages.setter('height')
        )

        self.sidebar_scroll.add_widget(self.sidebar_pages)
        self.sidebar.add_widget(self.sidebar_scroll)

        sidebar_library = BoxLayout(
            size_hint_y=None,
            height=dp(42),
            spacing=dp(5)
        )
        sidebar_library.add_widget(
            self.button('★', self.show_favorite_pages)
        )
        sidebar_library.add_widget(
            self.button('Recenti', self.show_recent_pages)
        )
        sidebar_library.add_widget(
            self.button('Cestino', self.show_trash)
        )
        self.sidebar.add_widget(sidebar_library)

        self.workspace_document = BoxLayout(
            orientation='vertical',
            spacing=dp(8)
        )

        self.workspace.add_widget(self.sidebar)
        self.workspace.add_widget(self.workspace_document)

        root.add_widget(self.workspace)

        # NESSO 3.5D - Responsive telefono / tablet
        Window.bind(size=self.apply_responsive_layout)
        Clock.schedule_once(
            lambda dt: self.apply_responsive_layout(),
            0
        )

        # NESSO 3.5B - documento nel workspace
        # Titolo documento
        self.page_title = TextInput(
            multiline=False,
            hint_text='Titolo della pagina',
            font_size='20sp',
            size_hint_y=None,
            height=dp(50),
            padding=[dp(12), dp(10)]
        )
        self.workspace_document.add_widget(self.page_title)

        # ---------------------------------------------------------
        # NESSO EDITOR 3.0 - modalità Scrivi / Anteprima
        # ---------------------------------------------------------
        self.editor_mode = 'write'

        mode_row = BoxLayout(
            size_hint_y=None,
            height=dp(44),
            spacing=dp(6)
        )

        self.write_mode_button = Button(
            text='Scrivi',
            font_size='14sp',
            background_normal='',
            background_color=(0.16, 0.36, 0.29, 1)
        )

        self.preview_mode_button = Button(
            text='Anteprima',
            font_size='14sp',
            background_normal='',
            background_color=(0.88, 0.91, 0.89, 1),
            color=(0.10, 0.18, 0.15, 1)
        )

        mode_row.add_widget(self.write_mode_button)
        mode_row.add_widget(self.preview_mode_button)
        self.workspace_document.add_widget(mode_row)

        # ---------------------------------------------------------
        # NESSO A4 EDITOR 3.0
        # Toolbar stile elaboratore di testi
        # ---------------------------------------------------------
        toolbar_scroll = ScrollView(
            size_hint_y=None,
            height=dp(52),
            do_scroll_y=False,
            bar_width=0
        )

        toolbar = BoxLayout(
            size_hint_x=None,
            height=dp(52),
            spacing=dp(5),
            padding=[dp(4), dp(4)]
        )
        toolbar.bind(minimum_width=toolbar.setter('width'))

        def tool(text, width=48):
            b = Button(
                text=text,
                size_hint=(None, None),
                width=dp(width),
                height=dp(44),
                font_size='16sp',
                background_normal='',
                background_color=(0.96, 0.97, 0.96, 1),
                color=(0.10, 0.18, 0.15, 1)
            )
            return b

        self.undo_button = tool('↶')
        self.redo_button = tool('↷')

        self.bold_button = tool('B')
        self.italic_button = tool('I')
        self.underline_button = tool('U')
        self.strike_button = tool('S')

        self.h1_button = tool('H1', 54)
        self.h2_button = tool('H2', 54)
        self.h3_button = tool('H3', 54)

        self.bullet_button = tool('•', 48)
        self.number_button = tool('1.', 48)

        self.outdent_button = tool('←', 48)
        self.indent_button = tool('→', 48)

        self.align_left_button = tool('≡', 48)
        self.align_center_button = tool('≣', 48)

        self.smaller_button = tool('A−', 54)
        self.larger_button = tool('A+', 54)

        for item in (
            self.undo_button,
            self.redo_button,
            self.bold_button,
            self.italic_button,
            self.underline_button,
            self.strike_button,
            self.h1_button,
            self.h2_button,
            self.h3_button,
            self.bullet_button,
            self.number_button,
            self.outdent_button,
            self.indent_button,
            self.align_left_button,
            self.align_center_button,
            self.smaller_button,
            self.larger_button
        ):
            toolbar.add_widget(item)

        self.editor_toolbar_buttons = (
            self.undo_button,
            self.redo_button,
            self.bold_button,
            self.italic_button,
            self.underline_button,
            self.strike_button,
            self.h1_button,
            self.h2_button,
            self.h3_button,
            self.bullet_button,
            self.number_button,
            self.outdent_button,
            self.indent_button,
            self.align_left_button,
            self.align_center_button,
            self.smaller_button,
            self.larger_button
        )

        toolbar_scroll.add_widget(toolbar)
        self.workspace_document.add_widget(toolbar_scroll)

        # ---------------------------------------------------------
        # Area documento
        # ---------------------------------------------------------
        self.document_scroll = ScrollView(
            do_scroll_x=False,
            bar_width=dp(5)
        )

        self.document_area = GridLayout(
            cols=1,
            size_hint_y=None,
            spacing=dp(24),
            padding=[dp(12), dp(18), dp(12), dp(30)]
        )
        self.document_area.bind(
            minimum_height=self.document_area.setter('height')
        )

        # Primo foglio A4.
        # Il rapporto 1 : 1.414 riproduce le proporzioni ISO A4.
        self.paper = BoxLayout(
            orientation='vertical',
            size_hint=(None, None),
            width=dp(690),
            height=dp(976),
            padding=[dp(55), dp(58), dp(55), dp(45)]
        )

        # Centra il foglio quando lo schermo è più largo.
        self.paper_row = BoxLayout(
            size_hint_y=None,
            height=dp(976)
        )
        self.paper_row.add_widget(Widget())
        self.paper_row.add_widget(self.paper)
        self.paper_row.add_widget(Widget())

        self.editor = TextInput(
            multiline=True,
            hint_text='Inizia a scrivere...',
            font_size='18sp',
            size_hint=(1, 1),
            padding=[dp(8), dp(8)],
            background_normal='',
            background_active='',
            background_color=(1, 1, 1, 1),
            foreground_color=(0.10, 0.13, 0.12, 1),
            cursor_color=(0.08, 0.30, 0.22, 1)
        )

        self.paper.add_widget(self.editor)

        self.preview_scroll = ScrollView(
            do_scroll_x=False,
            bar_width=dp(4)
        )

        self.preview_label = Label(
            text='',
            markup=True,
            color=(0.10, 0.13, 0.12, 1),
            font_size='18sp',
            halign='left',
            valign='top',
            size_hint_y=None,
            padding=[dp(8), dp(8)]
        )

        self.preview_label.bind(
            width=lambda obj, value: setattr(
                obj,
                'text_size',
                (max(dp(10), value - dp(16)), None)
            )
        )

        self.preview_label.bind(
            texture_size=lambda obj, value: setattr(
                obj,
                'height',
                max(value[1] + dp(20), dp(100))
            )
        )

        self.preview_scroll.add_widget(self.preview_label)

        self.page_number = Label(
            text='Pag. 1',
            color=(0.40, 0.43, 0.41, 1),
            font_size='12sp',
            halign='right',
            valign='middle',
            size_hint_y=None,
            height=dp(26)
        )
        self.page_number.bind(
            size=lambda obj, value: setattr(obj, 'text_size', value)
        )
        self.paper.add_widget(self.page_number)

        self.document_area.add_widget(self.paper_row)
        self.document_scroll.add_widget(self.document_area)
        self.workspace_document.add_widget(self.document_scroll)

        document_info = BoxLayout(
            size_hint_y=None,
            height=dp(42),
            spacing=dp(6)
        )

        self.stats_label = Label(
            text='0 parole  •  0 caratteri  •  0 righe',
            color=(0.28, 0.38, 0.34, 1),
            font_size='12sp',
            halign='left',
            valign='middle'
        )
        self.stats_label.bind(
            size=lambda obj, value: setattr(
                obj, 'text_size', value
            )
        )

        zoom_out = self.button(
            '−',
            lambda *_: self.zoom_document(-0.10)
        )
        zoom_out.width = dp(54)

        self.zoom_label = Label(
            text='100%',
            color=(0.18, 0.30, 0.26, 1),
            font_size='13sp',
            size_hint_x=None,
            width=dp(58)
        )

        zoom_in = self.button(
            '+',
            lambda *_: self.zoom_document(0.10)
        )
        zoom_in.width = dp(54)

        document_info.add_widget(self.stats_label)
        document_info.add_widget(zoom_out)
        document_info.add_widget(self.zoom_label)
        document_info.add_widget(zoom_in)

        self.workspace_document.add_widget(document_info)

        # Funzioni già operative nella prima versione.
        self.write_mode_button.bind(
            on_release=lambda *_: self.set_editor_mode('write')
        )
        self.preview_mode_button.bind(
            on_release=lambda *_: self.set_editor_mode('preview')
        )

        self.undo_button.bind(
            on_release=lambda *_: self.editor.do_undo()
        )
        self.redo_button.bind(
            on_release=lambda *_: self.editor.do_redo()
        )
        self.bold_button.bind(
            on_release=lambda *_: self.wrap_selection('**')
        )
        self.italic_button.bind(
            on_release=lambda *_: self.wrap_selection('*')
        )
        self.underline_button.bind(
            on_release=lambda *_: self.wrap_selection('<u>', '</u>')
        )
        self.strike_button.bind(
            on_release=lambda *_: self.wrap_selection('~~')
        )
        self.bullet_button.bind(
            on_release=lambda *_: self.insert_prefix('• ')
        )
        self.number_button.bind(
            on_release=lambda *_: self.insert_prefix('1. ')
        )
        self.indent_button.bind(
            on_release=lambda *_: self.insert_prefix('    ')
        )
        self.outdent_button.bind(
            on_release=self.outdent_current_line
        )
        self.h1_button.bind(
            on_release=lambda *_: self.set_heading(1)
        )
        self.h2_button.bind(
            on_release=lambda *_: self.set_heading(2)
        )
        self.h3_button.bind(
            on_release=lambda *_: self.set_heading(3)
        )
        self.align_left_button.bind(
            on_release=self.align_left
        )
        self.smaller_button.bind(
            on_release=lambda *_: self.change_font_size(-1)
        )
        self.larger_button.bind(
            on_release=lambda *_: self.change_font_size(1)
        )
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        self.status = Label(text='', color=(0.18, 0.35, 0.28, 1), font_size='13sp')
        row.add_widget(self.status)
        row.add_widget(self.button('Salva', self.save))
        self.workspace_document.add_widget(row)

        self.notebooks.bind(text=self.choose_notebook)
        self.pages.bind(text=self.choose_page)
        self.page_title.bind(text=self.changed)
        self.editor.bind(text=self.changed)
        self.editor.bind(
            text=lambda *_: self.update_preview()
            if self.editor_mode == 'preview' else None
        )
        self.load_notebook(self.store.notebooks()[0][0])
        return root

    def render_preview_text(self):
        text = self.editor.text or ''

        # Protegge il markup Kivy già presente negli appunti.
        text = text.replace('&', '&amp;')
        text = text.replace('[', '&bl;')
        text = text.replace(']', '&br;')

        lines = []

        import re

        for line in text.split('\n'):
            is_heading = False

            if line.startswith('### '):
                line = '[size=22sp][b]' + line[4:] + '[/b][/size]'
                is_heading = True

            elif line.startswith('## '):
                line = '[size=26sp][b]' + line[3:] + '[/b][/size]'
                is_heading = True

            elif line.startswith('# '):
                line = '[size=32sp][b]' + line[2:] + '[/b][/size]'
                is_heading = True

            elif re.match(r'^\s*•\s+', line):
                match = re.match(r'^(\s*)•\s+(.*)$', line)
                if match:
                    indent, content = match.groups()
                    line = indent + '•  ' + content

            elif re.match(r'^\s*\d+\.\s+', line):
                match = re.match(
                    r'^(\s*)(\d+)\.\s+(.*)$',
                    line
                )
                if match:
                    indent, number, content = match.groups()
                    line = (
                        indent
                        + '[b]'
                        + number
                        + '.[/b]  '
                        + content
                    )

            if is_heading and lines and lines[-1] != '':
                lines.append('')

            lines.append(line)

            if is_heading:
                lines.append('')


        text = '\n'.join(lines)

        text = re.sub(
            r'\*\*(.+?)\*\*',
            r'[b]\1[/b]',
            text,
            flags=re.DOTALL
        )
        text = re.sub(
            r'(?<!\*)\*([^*\n]+?)\*(?!\*)',
            r'[i]\1[/i]',
            text
        )
        text = re.sub(
            r'<u>(.+?)</u>',
            r'[u]\1[/u]',
            text,
            flags=re.DOTALL
        )
        text = re.sub(
            r'~~(.+?)~~',
            r'[s]\1[/s]',
            text,
            flags=re.DOTALL
        )

        return text

    def update_preview(self):
        if not hasattr(self, 'preview_label'):
            return

        self.preview_label.text = self.render_preview_text()

    def set_editor_mode(self, mode):
        if mode not in ('write', 'preview'):
            return

        if mode == self.editor_mode:
            return

        if mode == 'preview':
            self.update_preview()

            if self.editor.parent is self.paper:
                self.paper.remove_widget(self.editor)

            if self.preview_scroll.parent is None:
                self.paper.add_widget(self.preview_scroll, index=1)

            self.editor_mode = 'preview'

            self.write_mode_button.background_color = (
                0.88, 0.91, 0.89, 1
            )
            self.write_mode_button.color = (
                0.10, 0.18, 0.15, 1
            )
            self.preview_mode_button.background_color = (
                0.16, 0.36, 0.29, 1
            )
            self.preview_mode_button.color = (1, 1, 1, 1)

            for button in self.editor_toolbar_buttons:
                button.disabled = True

        else:
            if self.preview_scroll.parent is self.paper:
                self.paper.remove_widget(self.preview_scroll)

            if self.editor.parent is None:
                self.paper.add_widget(self.editor, index=1)

            self.editor_mode = 'write'

            self.write_mode_button.background_color = (
                0.16, 0.36, 0.29, 1
            )
            self.write_mode_button.color = (1, 1, 1, 1)
            self.preview_mode_button.background_color = (
                0.88, 0.91, 0.89, 1
            )
            self.preview_mode_button.color = (
                0.10, 0.18, 0.15, 1
            )

            for button in self.editor_toolbar_buttons:
                button.disabled = False

        self.update_document_info()

    def button(self, text, callback):
        button = Button(text=text, size_hint_x=None, width=dp(112),
                        font_size='14sp', background_normal='',
                        background_color=(0.16, 0.36, 0.29, 1))
        button.bind(on_release=callback)
        return button

    def selected_line_range(self):
        text = self.editor.text
        a = min(self.editor.selection_from, self.editor.selection_to)
        b = max(self.editor.selection_from, self.editor.selection_to)

        if a == b:
            a = self.editor.cursor_index()
            b = a

        start = text.rfind('\n', 0, a) + 1

        if b > start and b <= len(text) and text[b - 1:b] == '\n':
            end = b - 1
        else:
            end = text.find('\n', b)
            if end == -1:
                end = len(text)

        return start, end

    def transform_selected_lines(self, transform):
        if self.editor.disabled:
            return

        start, end = self.selected_line_range()
        block = self.editor.text[start:end]
        lines = block.split('\n')
        new_lines = transform(lines)
        replacement = '\n'.join(new_lines)

        self.editor.text = (
            self.editor.text[:start]
            + replacement
            + self.editor.text[end:]
        )

        self.editor.select_text(start, start + len(replacement))
        self.editor.focus = True

    def strip_list_prefix(self, line):
        import re

        indent = line[:len(line) - len(line.lstrip(' \t'))]
        content = line[len(indent):]

        if content.startswith('• '):
            content = content[2:]
        else:
            content = re.sub(r'^\d+\.\s+', '', content)

        return indent, content

    def toggle_bullet_list(self):
        def transform(lines):
            parsed = [self.strip_list_prefix(line) for line in lines]

            all_bullets = all(
                line[len(indent):].startswith('• ')
                for line, (indent, _) in zip(lines, parsed)
                if line.strip()
            )

            result = []
            for line, (indent, content) in zip(lines, parsed):
                if not line.strip():
                    result.append(line)
                elif all_bullets:
                    result.append(indent + content)
                else:
                    result.append(indent + '• ' + content)

            return result

        self.transform_selected_lines(transform)

    def toggle_numbered_list(self):
        import re

        def transform(lines):
            parsed = [self.strip_list_prefix(line) for line in lines]

            non_empty = [line for line in lines if line.strip()]
            all_numbered = bool(non_empty) and all(
                re.match(
                    r'^\s*\d+\.\s+',
                    line
                )
                for line in non_empty
            )

            result = []
            number = 1

            for line, (indent, content) in zip(lines, parsed):
                if not line.strip():
                    result.append(line)
                elif all_numbered:
                    result.append(indent + content)
                else:
                    result.append(
                        f'{indent}{number}. {content}'
                    )
                    number += 1

            return result

        self.transform_selected_lines(transform)

    def insert_prefix(self, prefix):
        if prefix == '• ':
            self.toggle_bullet_list()
            return

        if prefix == '1. ':
            self.toggle_numbered_list()
            return

        if prefix == '    ':
            self.transform_selected_lines(
                lambda lines: ['    ' + line for line in lines]
            )
            return

        pos = self.editor.cursor_index()
        self.editor.text = (
            self.editor.text[:pos]
            + prefix
            + self.editor.text[pos:]
        )
        self.editor.cursor = self.editor.get_cursor_from_index(
            pos + len(prefix)
        )
        self.editor.focus = True

    def wrap_selection(self, left, right=None):
        if self.editor.disabled:
            return

        if right is None:
            right = left

        start = min(self.editor.selection_from, self.editor.selection_to)
        end = max(self.editor.selection_from, self.editor.selection_to)

        if start != end:
            selected = self.editor.text[start:end]
            replacement = left + selected + right

            self.editor.text = (
                self.editor.text[:start]
                + replacement
                + self.editor.text[end:]
            )

            self.editor.select_text(
                start + len(left),
                start + len(left) + len(selected)
            )
        else:
            pos = self.editor.cursor_index()
            replacement = left + right

            self.editor.text = (
                self.editor.text[:pos]
                + replacement
                + self.editor.text[pos:]
            )

            self.editor.cursor = self.editor.get_cursor_from_index(
                pos + len(left)
            )

        self.editor.focus = True

    def current_line_bounds(self):
        text = self.editor.text
        pos = self.editor.cursor_index()
        start = text.rfind('\n', 0, pos) + 1
        end = text.find('\n', pos)
        if end == -1:
            end = len(text)
        return start, end

    def set_heading(self, level):
        if self.editor.disabled:
            return

        prefix = ('#' * level) + ' '

        def heading_lines(lines):
            result = []

            for line in lines:
                stripped = line.lstrip()

                for old_prefix in ('### ', '## ', '# '):
                    if stripped.startswith(old_prefix):
                        stripped = stripped[len(old_prefix):]
                        break

                result.append(prefix + stripped)

            return result

        self.transform_selected_lines(heading_lines)

    def align_left(self, *args):
        if self.editor.disabled:
            return

        def left_lines(lines):
            return [line.lstrip(' \t') for line in lines]

        self.transform_selected_lines(left_lines)

    def outdent_current_line(self, *args):
        def remove_indent(lines):
            result = []

            for line in lines:
                if line.startswith('    '):
                    line = line[4:]
                elif line.startswith('\t'):
                    line = line[1:]
                elif line.startswith(' '):
                    line = line[1:]

                result.append(line)

            return result

        self.transform_selected_lines(remove_indent)

    def change_font_size(self, delta):
        current = float(self.editor.font_size)
        self.editor.font_size = max(dp(12), min(dp(34), current + dp(delta)))
        self.editor.focus = True

    def document_stats(self):
        text = self.editor.text if hasattr(self, 'editor') else ''
        words = len(text.split())
        chars = len(text)
        lines = text.count('\n') + 1 if text else 0
        return words, chars, lines

    def update_document_info(self):
        self.refresh_workspace_sidebar()
        if not hasattr(self, 'editor'):
            return

        words, chars, lines = self.document_stats()

        if hasattr(self, 'stats_label'):
            self.stats_label.text = (
                f'{words} parole  •  {chars} caratteri  •  {lines} righe'
            )

        if self.page_id is not None and hasattr(self, 'page_number'):
            position, total = self.store.page_position(self.page_id)
            self.page_number.text = (
                f'Pagina {position} di {total}'
                if total else 'Pagina'
            )

    def previous_page(self, *args):
        if self.page_id is None or not self.save():
            return

        pages = self.store.pages(self.notebook_id)
        ids = [page_id for page_id, _ in pages]

        try:
            index = ids.index(self.page_id)
        except ValueError:
            return

        if index > 0:
            self.load_page(ids[index - 1])

    def next_page(self, *args):
        if self.page_id is None or not self.save():
            return

        pages = self.store.pages(self.notebook_id)
        ids = [page_id for page_id, _ in pages]

        try:
            index = ids.index(self.page_id)
        except ValueError:
            return

        if index < len(ids) - 1:
            self.load_page(ids[index + 1])

    def duplicate_current_page(self, *args):
        if self.page_id is None or not self.save():
            return

        try:
            target = self.store.duplicate_page(self.page_id)
        except (sqlite3.Error, ValueError) as ex:
            self.status.text = 'Duplicazione non riuscita'
            self.message('Duplica pagina', str(ex))
            return

        self.load_page(target)
        self.status.text = 'Pagina duplicata'

    def move_page_up(self, *args):
        if self.page_id is None or not self.save():
            return

        if self.store.move_page_by(self.page_id, -1):
            self.refresh_pages()
            self.update_document_info()
            self.status.text = 'Pagina spostata in alto'

    def move_page_down(self, *args):
        if self.page_id is None or not self.save():
            return

        if self.store.move_page_by(self.page_id, 1):
            self.refresh_pages()
            self.update_document_info()
            self.status.text = 'Pagina spostata in basso'

    def move_page_to_notebook(self, *args):
        if self.page_id is None or not self.save():
            return

        notebooks = [
            (notebook_id, name)
            for notebook_id, name in self.store.notebooks()
            if notebook_id != self.notebook_id
        ]

        if not notebooks:
            self.message(
                'Sposta pagina',
                'Crea prima un altro quaderno.'
            )
            return

        content = BoxLayout(
            orientation='vertical',
            spacing=dp(12),
            padding=dp(12)
        )

        labels = {
            name: notebook_id
            for notebook_id, name in notebooks
        }

        selector = Spinner(
            text=notebooks[0][1],
            values=[name for _, name in notebooks],
            font_size='16sp',
            size_hint_y=None,
            height=dp(48)
        )
        content.add_widget(selector)

        actions = BoxLayout(
            size_hint_y=None,
            height=dp(48),
            spacing=dp(8)
        )

        popup = Popup(
            title='Sposta pagina',
            content=content,
            size_hint=(0.9, None),
            height=dp(230)
        )

        def apply_move(*_):
            target_notebook = labels.get(selector.text)

            if target_notebook is None:
                return

            old_notebook = self.notebook_id
            current_page = self.page_id

            try:
                self.store.move_page(
                    current_page,
                    target_notebook
                )
            except (sqlite3.Error, ValueError) as ex:
                popup.dismiss()
                self.message('Sposta pagina', str(ex))
                return

            popup.dismiss()

            remaining = self.store.pages(old_notebook)

            if remaining:
                self.load_page(remaining[0][0])
            else:
                self.load_notebook(old_notebook)

            self.status.text = 'Pagina spostata'

        actions.add_widget(
            self.button('Annulla', lambda *_: popup.dismiss())
        )
        actions.add_widget(
            self.button('Sposta', apply_move)
        )

        content.add_widget(actions)
        popup.open()

    def apply_responsive_layout(self, *args):
        if not hasattr(self, 'workspace'):
            return

        width_dp = Window.width / max(dp(1), 1)

        # TABLET ORIZZONTALE / SCHERMO GRANDE
        if width_dp >= 900:
            self.sidebar.width = dp(260)
            self.sidebar.opacity = 1
            self.sidebar.disabled = False

            if self.sidebar.parent is None:
                self.workspace.add_widget(
                    self.sidebar,
                    index=len(self.workspace.children)
                )

            target_width = dp(690)

        # TABLET VERTICALE / SCHERMO MEDIO
        elif width_dp >= 600:
            self.sidebar.width = dp(205)
            self.sidebar.opacity = 1
            self.sidebar.disabled = False

            if self.sidebar.parent is None:
                self.workspace.add_widget(
                    self.sidebar,
                    index=len(self.workspace.children)
                )

            available = max(
                dp(360),
                Window.width - self.sidebar.width - dp(55)
            )
            target_width = min(dp(620), available)

        # TELEFONO
        else:
            if self.sidebar.parent is self.workspace:
                self.workspace.remove_widget(self.sidebar)

            self.sidebar.disabled = True

            target_width = max(
                dp(280),
                Window.width - dp(32)
            )

        # Il foglio mantiene sempre il rapporto A4 210 x 297.
        target_width = max(dp(280), target_width)
        target_height = target_width * (297.0 / 210.0)

        if hasattr(self, 'paper'):
            self.paper.size_hint = (None, None)
            self.paper.width = target_width
            self.paper.height = target_height

        if hasattr(self, 'document_area'):
            self.document_area.size_hint_x = 1

        if hasattr(self, 'page_title'):
            if width_dp >= 900:
                self.page_title.font_size = '20sp'
            elif width_dp >= 600:
                self.page_title.font_size = '19sp'
            else:
                self.page_title.font_size = '17sp'

        if hasattr(self, 'sidebar_notebook_title'):
            self.sidebar_notebook_title.font_size = (
                '18sp' if width_dp >= 900 else '16sp'
            )

    def refresh_workspace_sidebar(self):
        if not hasattr(self, 'sidebar_pages'):
            return

        self.sidebar_pages.clear_widgets()

        if self.notebook_id is None:
            self.sidebar_notebook_title.text = 'Nessun quaderno'
            return

        notebook_name = next(
            (
                name
                for notebook_id, name in self.store.notebooks()
                if notebook_id == self.notebook_id
            ),
            'Quaderno'
        )

        self.sidebar_notebook_title.text = notebook_name

        rows = self.store.db.execute(
            '''SELECT id,title,parent_id,favorite
               FROM pages
               WHERE notebook_id=?
                 AND trashed=0
               ORDER BY sort_order,id''',
            (self.notebook_id,)
        ).fetchall()

        if not rows:
            self.sidebar_pages.add_widget(
                Label(
                    text='Nessuna pagina',
                    color=(0.35, 0.40, 0.38, 1),
                    size_hint_y=None,
                    height=dp(44)
                )
            )
            return

        existing = {row[0] for row in rows}

        def add_entry(page_id, title, parent_id, favorite):
            is_child = (
                parent_id is not None
                and parent_id in existing
            )

            prefix = '    ↳ ' if is_child else ''
            star = '★ ' if favorite else ''

            text = (
                prefix
                + star
                + (title or 'Senza titolo')
            )

            def open_sidebar_page(*_, target=page_id):
                if target == self.page_id:
                    return
                if not self.save():
                    return
                self.load_page(target)

            button = self.button(
                text,
                open_sidebar_page
            )
            button.size_hint_y = None
            button.height = dp(46)

            if page_id == self.page_id:
                button.text = '› ' + button.text

            self.sidebar_pages.add_widget(button)

        # Prima pagine principali, con le relative sottopagine.
        children = {}

        for row in rows:
            children.setdefault(row[2], []).append(row)

        rendered = set()

        for row in rows:
            page_id, title, parent_id, favorite = row

            if parent_id is not None and parent_id in existing:
                continue

            add_entry(*row)
            rendered.add(page_id)

            for child in children.get(page_id, []):
                add_entry(*child)
                rendered.add(child[0])

        # Sicurezza per eventuali gerarchie legacy/non valide.
        for row in rows:
            if row[0] not in rendered:
                add_entry(*row)

    def set_current_subpage(self, *args):
        if self.page_id is None or not self.save():
            return

        pages = [
            (page_id, title)
            for page_id, title in self.store.pages(self.notebook_id)
            if page_id != self.page_id
        ]

        current = self.store.db.execute(
            'SELECT parent_id FROM pages WHERE id=?',
            (self.page_id,)
        ).fetchone()

        current_parent = current[0] if current else None

        content = BoxLayout(
            orientation='vertical',
            spacing=dp(12),
            padding=dp(12)
        )

        choices = ['Nessuna — pagina principale']
        mapping = {
            'Nessuna — pagina principale': None
        }

        for page_id, title in pages:
            label = f'{page_id} - {title or "Senza titolo"}'
            choices.append(label)
            mapping[label] = page_id

        selected = choices[0]

        if current_parent is not None:
            for label, page_id in mapping.items():
                if page_id == current_parent:
                    selected = label
                    break

        selector = Spinner(
            text=selected,
            values=choices,
            font_size='16sp',
            size_hint_y=None,
            height=dp(48)
        )
        content.add_widget(selector)

        info = Label(
            text=(
                'Scegli la pagina principale.\n'
                'La pagina corrente verrà mostrata come sottopagina.'
            ),
            color=(0.20, 0.25, 0.23, 1),
            halign='center',
            valign='middle'
        )
        info.bind(
            size=lambda obj, value: setattr(
                obj, 'text_size', value
            )
        )
        content.add_widget(info)

        actions = BoxLayout(
            size_hint_y=None,
            height=dp(48),
            spacing=dp(8)
        )

        popup = Popup(
            title='Organizza come sottopagina',
            content=content,
            size_hint=(0.92, None),
            height=dp(300)
        )

        def apply_parent(*_):
            parent_id = mapping.get(selector.text)

            try:
                self.store.set_parent_page(
                    self.page_id,
                    parent_id
                )
            except (sqlite3.Error, ValueError) as ex:
                popup.dismiss()
                self.message('Sottopagina', str(ex))
                return

            popup.dismiss()
            self.refresh_pages()

            if parent_id is None:
                self.status.text = 'Pagina riportata al livello principale'
            else:
                self.status.text = 'Sottopagina impostata'

        actions.add_widget(
            self.button('Annulla', lambda *_: popup.dismiss())
        )
        actions.add_widget(
            self.button('Applica', apply_parent)
        )

        content.add_widget(actions)
        popup.open()

    def toggle_current_favorite(self, *args):
        if self.page_id is None or not self.save():
            return

        try:
            self.store.favorite_page(self.page_id)
        except sqlite3.Error as ex:
            self.message('Preferiti', str(ex))
            return

        if self.store.page_is_favorite(self.page_id):
            self.status.text = '★ Pagina aggiunta ai preferiti'
        else:
            self.status.text = 'Pagina rimossa dai preferiti'

    def show_page_collection(self, title, rows, trash=False):
        content = BoxLayout(
            orientation='vertical',
            spacing=dp(8),
            padding=dp(10)
        )

        scroll = ScrollView(do_scroll_x=False)
        items = GridLayout(
            cols=1,
            spacing=dp(6),
            size_hint_y=None
        )
        items.bind(
            minimum_height=items.setter('height')
        )

        popup = Popup(
            title=title,
            content=content,
            size_hint=(0.94, 0.88)
        )

        if not rows:
            items.add_widget(
                Label(
                    text='Nessuna pagina',
                    size_hint_y=None,
                    height=dp(50),
                    color=(0.20, 0.25, 0.23, 1)
                )
            )

        for page_id, page_title, notebook_name in rows:
            row = BoxLayout(
                size_hint_y=None,
                height=dp(54),
                spacing=dp(6)
            )

            label = (
                (page_title or 'Senza titolo')
                + '  •  '
                + notebook_name
            )

            def open_page(
                *_,
                target=page_id,
                is_trash=trash
            ):
                if is_trash:
                    return

                location = self.store.db.execute(
                    'SELECT notebook_id FROM pages WHERE id=?',
                    (target,)
                ).fetchone()

                if location is None:
                    return

                popup.dismiss()
                self.load_notebook(location[0])
                self.load_page(target)

            open_button = self.button(
                label,
                open_page
            )
            row.add_widget(open_button)

            if trash:
                def restore(
                    *_,
                    target=page_id
                ):
                    try:
                        self.store.restore_page(target)
                    except sqlite3.Error as ex:
                        self.message('Cestino', str(ex))
                        return

                    popup.dismiss()

                    location = self.store.db.execute(
                        'SELECT notebook_id FROM pages WHERE id=?',
                        (target,)
                    ).fetchone()

                    if location:
                        self.load_notebook(location[0])
                        self.load_page(target)

                    self.status.text = 'Pagina ripristinata'

                restore_button = self.button(
                    'Ripristina',
                    restore
                )
                restore_button.size_hint_x = 0.34
                row.add_widget(restore_button)

            items.add_widget(row)

        scroll.add_widget(items)
        content.add_widget(scroll)

        close = self.button(
            'Chiudi',
            lambda *_: popup.dismiss()
        )
        close.size_hint_y = None
        close.height = dp(48)
        content.add_widget(close)

        popup.open()

    def show_favorite_pages(self, *args):
        self.show_page_collection(
            '★ Preferiti',
            self.store.favorite_pages()
        )

    def show_recent_pages(self, *args):
        self.show_page_collection(
            'Pagine recenti',
            self.store.recent_pages(20)
        )

    def show_trash(self, *args):
        self.show_page_collection(
            'Cestino',
            self.store.trashed_pages(),
            trash=True
        )

    def rename_current_page(self, *args):
        if self.page_id is None or not self.save():
            return

        content = BoxLayout(
            orientation='vertical',
            spacing=dp(12),
            padding=dp(12)
        )

        name = TextInput(
            text=self.page_title.text,
            multiline=False,
            hint_text='Titolo della pagina',
            font_size='18sp',
            size_hint_y=None,
            height=dp(48)
        )

        content.add_widget(name)

        actions = BoxLayout(
            size_hint_y=None,
            height=dp(48),
            spacing=dp(8)
        )

        popup = Popup(
            title='Rinomina pagina',
            content=content,
            size_hint=(0.9, None),
            height=dp(230)
        )

        def apply_rename(*_):
            title = name.text.strip()

            if not title:
                name.hint_text = 'Inserisci un titolo'
                return

            try:
                self.store.rename_page(self.page_id, title)
            except (sqlite3.Error, ValueError) as ex:
                popup.dismiss()
                self.status.text = 'Rinomina non riuscita'
                self.message('Rinomina pagina', str(ex))
                return

            self.loading = True
            self.page_title.text = title
            self.loading = False

            self.refresh_pages()
            self.status.text = 'Pagina rinominata'
            popup.dismiss()

        actions.add_widget(
            self.button('Annulla', lambda *_: popup.dismiss())
        )
        actions.add_widget(
            self.button('Rinomina', apply_rename)
        )

        content.add_widget(actions)
        name.bind(on_text_validate=apply_rename)

        popup.open()
        Clock.schedule_once(
            lambda *_: setattr(name, 'focus', True),
            0.1
        )

    def confirm_delete_current_page(self, *args):
        if self.page_id is None:
            return

        content = BoxLayout(
            orientation='vertical',
            spacing=dp(12),
            padding=dp(12)
        )

        title = self.page_title.text.strip() or 'Senza titolo'

        warning = Label(
            text=(
                'Eliminare la pagina "' + title + '"?\n\n'
                'Potrai ripristinarla dal Cestino.'
            ),
            color=(0.15, 0.18, 0.17, 1),
            halign='center',
            valign='middle'
        )
        warning.bind(
            size=lambda obj, value: setattr(
                obj,
                'text_size',
                value
            )
        )

        content.add_widget(warning)

        actions = BoxLayout(
            size_hint_y=None,
            height=dp(48),
            spacing=dp(8)
        )

        popup = Popup(
            title='Elimina pagina',
            content=content,
            size_hint=(0.9, None),
            height=dp(260)
        )

        def delete_confirmed(*_):
            current_id = self.page_id

            pages_before = self.store.pages(self.notebook_id)
            ids_before = [
                page_id
                for page_id, _ in pages_before
            ]

            try:
                old_index = ids_before.index(current_id)
            except ValueError:
                old_index = 0

            try:
                self.store.trash_page(current_id)
            except sqlite3.Error as ex:
                popup.dismiss()
                self.status.text = 'Eliminazione non riuscita'
                self.message('Elimina pagina', str(ex))
                return

            popup.dismiss()

            pages_after = self.store.pages(self.notebook_id)

            if not pages_after:
                self.load_notebook(self.notebook_id)
                self.status.text = 'Pagina spostata nel cestino'
                return

            ids_after = [
                page_id
                for page_id, _ in pages_after
            ]

            target_index = min(
                old_index,
                len(ids_after) - 1
            )

            self.load_page(ids_after[target_index])
            self.status.text = 'Pagina spostata nel cestino'

        actions.add_widget(
            self.button('Annulla', lambda *_: popup.dismiss())
        )
        actions.add_widget(
            self.button('Elimina', delete_confirmed)
        )

        content.add_widget(actions)
        popup.open()

    def search_current_notebook(self, *args):
        content = BoxLayout(
            orientation='vertical',
            spacing=dp(10),
            padding=dp(12)
        )

        query = TextInput(
            multiline=False,
            hint_text='Cerca negli appunti...',
            font_size='18sp',
            size_hint_y=None,
            height=dp(52)
        )

        results = BoxLayout(
            orientation='vertical',
            spacing=dp(6),
            size_hint_y=None
        )
        results.bind(minimum_height=results.setter('height'))

        scroll = ScrollView()
        scroll.add_widget(results)

        content.add_widget(query)
        content.add_widget(scroll)

        popup = Popup(
            title='Cerca nel quaderno',
            content=content,
            size_hint=(0.94, 0.82)
        )

        def run_search(*_):
            results.clear_widgets()

            matches = self.store.search_pages(
                query.text,
                self.notebook_id
            )

            if not matches:
                results.add_widget(Label(
                    text='Nessun risultato',
                    size_hint_y=None,
                    height=dp(48)
                ))
                return

            for page_id, title, notebook_name in matches:
                result_button = Button(
                    text=title or 'Senza titolo',
                    size_hint_y=None,
                    height=dp(52),
                    background_normal='',
                    background_color=(0.94, 0.96, 0.94, 1),
                    color=(0.10, 0.22, 0.18, 1)
                )

                def open_result(instance, target=page_id):
                    if self.save():
                        popup.dismiss()
                        self.load_page(target)

                result_button.bind(on_release=open_result)
                results.add_widget(result_button)

        query.bind(on_text_validate=run_search)

        actions = BoxLayout(
            size_hint_y=None,
            height=dp(48),
            spacing=dp(8)
        )
        actions.add_widget(self.button('Cerca', run_search))
        actions.add_widget(self.button('Chiudi', popup.dismiss))
        content.add_widget(actions)

        popup.open()
        Clock.schedule_once(
            lambda dt: setattr(query, 'focus', True),
            0.1
        )

    def zoom_document(self, delta):
        current = getattr(self, 'document_zoom', 1.0)
        self.document_zoom = max(
            0.65,
            min(1.25, current + delta)
        )

        self.paper.width = dp(690) * self.document_zoom
        self.paper.height = dp(976) * self.document_zoom
        self.paper_row.height = self.paper.height

        if hasattr(self, 'zoom_label'):
            self.zoom_label.text = (
                f'{round(self.document_zoom * 100)}%'
            )

    def changed(self, *args):
        if self.loading:
            return
        self.dirty = True
        self.status.text = 'Salvataggio...'
        self.update_document_info()
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
        page_rows = self.store.pages(self.notebook_id)
        page_ids = {page_id for page_id, _ in page_rows}

        parents = {
            page_id: parent_id
            for page_id, parent_id in self.store.db.execute(
                '''SELECT id,parent_id FROM pages
                   WHERE notebook_id=? AND trashed=0''',
                (self.notebook_id,)
            ).fetchall()
        }

        self.page_map = {}

        for page_id, title in page_rows:
            prefix = '↳ ' if parents.get(page_id) in page_ids else ''
            label = (
                f'{prefix}{page_id} - '
                f'{title or "Senza titolo"}'
            )
            self.page_map[label] = page_id
        self.pages.values = list(self.page_map)
        self.refresh_workspace_sidebar()
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
        self.update_document_info()

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

    def rename_current_notebook(self, *args):
        if self.notebook_id is None or not self.save():
            return

        current_name = next(
            (
                name
                for notebook_id, name in self.store.notebooks()
                if notebook_id == self.notebook_id
            ),
            ''
        )

        content = BoxLayout(
            orientation='vertical',
            spacing=dp(12),
            padding=dp(12)
        )

        name = TextInput(
            text=current_name,
            multiline=False,
            hint_text='Nome del quaderno',
            font_size='18sp',
            size_hint_y=None,
            height=dp(48)
        )
        content.add_widget(name)

        actions = BoxLayout(
            size_hint_y=None,
            height=dp(48),
            spacing=dp(8)
        )

        popup = Popup(
            title='Rinomina quaderno',
            content=content,
            size_hint=(0.9, None),
            height=dp(230)
        )

        def apply_rename(*_):
            new_name = name.text.strip()

            if not new_name:
                name.text = ''
                name.hint_text = 'Inserisci un nome'
                return

            try:
                self.store.rename_notebook(
                    self.notebook_id,
                    new_name
                )
            except (sqlite3.Error, ValueError) as ex:
                popup.dismiss()
                self.status.text = 'Rinomina non riuscita'
                self.message('Rinomina quaderno', str(ex))
                return

            current_id = self.notebook_id
            popup.dismiss()
            self.load_notebook(current_id)
            self.status.text = 'Quaderno rinominato'

        actions.add_widget(
            self.button('Annulla', lambda *_: popup.dismiss())
        )
        actions.add_widget(
            self.button('Rinomina', apply_rename)
        )

        content.add_widget(actions)
        name.bind(on_text_validate=apply_rename)

        popup.open()
        Clock.schedule_once(
            lambda *_: setattr(name, 'focus', True),
            0.1
        )

    def confirm_delete_current_notebook(self, *args):
        if self.notebook_id is None:
            return

        notebooks = self.store.notebooks()

        if len(notebooks) <= 1:
            self.message(
                'Elimina quaderno',
                'Non puoi eliminare l\'ultimo quaderno.'
            )
            return

        current_name = next(
            (
                name
                for notebook_id, name in notebooks
                if notebook_id == self.notebook_id
            ),
            'Quaderno'
        )

        content = BoxLayout(
            orientation='vertical',
            spacing=dp(12),
            padding=dp(12)
        )

        warning = Label(
            text=(
                'Eliminare il quaderno "' + current_name + '"?\n\n'
                'Verranno eliminate anche tutte le sue pagine.\n'
                'Questa operazione non può essere annullata.'
            ),
            color=(0.15, 0.18, 0.17, 1),
            halign='center',
            valign='middle'
        )
        warning.bind(
            size=lambda obj, value: setattr(
                obj,
                'text_size',
                value
            )
        )
        content.add_widget(warning)

        actions = BoxLayout(
            size_hint_y=None,
            height=dp(48),
            spacing=dp(8)
        )

        popup = Popup(
            title='Elimina quaderno',
            content=content,
            size_hint=(0.9, None),
            height=dp(300)
        )

        def delete_confirmed(*_):
            current_id = self.notebook_id

            notebooks_before = self.store.notebooks()
            ids_before = [
                notebook_id
                for notebook_id, _ in notebooks_before
            ]

            try:
                old_index = ids_before.index(current_id)
            except ValueError:
                old_index = 0

            try:
                self.store.delete_notebook(current_id)
            except (sqlite3.Error, ValueError) as ex:
                popup.dismiss()
                self.status.text = 'Eliminazione non riuscita'
                self.message('Elimina quaderno', str(ex))
                return

            notebooks_after = self.store.notebooks()

            if not notebooks_after:
                popup.dismiss()
                self.status.text = 'Errore: nessun quaderno disponibile'
                return

            target_index = min(
                old_index,
                len(notebooks_after) - 1
            )
            target_id = notebooks_after[target_index][0]

            popup.dismiss()
            self.load_notebook(target_id)
            self.status.text = 'Quaderno eliminato'

        actions.add_widget(
            self.button('Annulla', lambda *_: popup.dismiss())
        )
        actions.add_widget(
            self.button('Elimina', delete_confirmed)
        )

        content.add_widget(actions)
        popup.open()

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
