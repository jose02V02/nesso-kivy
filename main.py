from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput


class NessoApp(App):

    def build(self):
        root = BoxLayout(
            orientation="vertical",
            padding=20,
            spacing=15
        )

        titolo = Label(
            text="NESSO",
            font_size="32sp",
            size_hint_y=None,
            height=60
        )

        pagina = TextInput(
            hint_text="Inizia a scrivere la tua pagina...",
            multiline=True,
            font_size="20sp"
        )

        root.add_widget(titolo)
        root.add_widget(pagina)

        return root


if __name__ == "__main__":
    NessoApp().run()
