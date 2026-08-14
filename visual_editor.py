from __future__ import annotations

import streamlit as st


_visual_editor = st.components.v2.component(
    name="visual_html_editor",
    html="""
    <div class="toolbar" role="toolbar" aria-label="Formatowanie opisu">
      <select aria-label="Format akapitu">
        <option value="p">Akapit</option>
        <option value="h2">Nagłówek H2</option>
        <option value="h3">Nagłówek H3</option>
      </select>
      <button type="button" data-command="bold" aria-label="Pogrubienie"><b>B</b></button>
      <button type="button" data-command="createLink" aria-label="Dodaj link">Link</button>
      <button type="button" data-command="unlink" aria-label="Usuń link">Usuń link</button>
      <button type="button" data-command="undo" aria-label="Cofnij">↶</button>
      <button type="button" data-command="redo" aria-label="Ponów">↷</button>
      <button type="button" class="source-toggle" aria-label="Przełącz widok HTML">&lt;/&gt;</button>
    </div>
    <div class="editor" contenteditable="true" role="textbox" aria-multiline="true"></div>
    <textarea class="source" aria-label="Kod HTML opisu" spellcheck="false"></textarea>
    """,
    css="""
    :host { display: block; color: var(--st-text-color); }
    .toolbar { display: flex; flex-wrap: wrap; gap: .35rem; padding: .55rem; border: 1px solid var(--st-border-color); border-bottom: 0; border-radius: .5rem .5rem 0 0; background: var(--st-secondary-background-color); }
    button, select { min-height: 2.2rem; padding: .35rem .65rem; border: 1px solid var(--st-border-color); border-radius: .35rem; background: var(--st-background-color); color: var(--st-text-color); cursor: pointer; }
    button:hover, select:hover { border-color: var(--st-primary-color); }
    .source-toggle { margin-left: auto; }
    .editor, .source { box-sizing: border-box; width: 100%; min-height: 22rem; padding: 1rem 1.25rem; border: 1px solid var(--st-border-color); border-radius: 0 0 .5rem .5rem; background: var(--st-background-color); color: var(--st-text-color); font: inherit; line-height: 1.55; overflow: auto; }
    .editor:focus, .source:focus { outline: 2px solid var(--st-primary-color); outline-offset: -2px; }
    .editor h2 { font-size: 1.45rem; margin: 1.25rem 0 .5rem; }
    .editor h3 { font-size: 1.2rem; margin: 1.1rem 0 .5rem; }
    .editor p { margin: .65rem 0; }
    .editor b { font-weight: 700; }
    .editor a { color: var(--st-primary-color); text-decoration: underline; }
    .source { display: none; resize: vertical; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .9rem; }
    :host(.show-source) .editor { display: none; }
    :host(.show-source) .source { display: block; }
    """,
    js="""
    export default function(component) {
      const { data, parentElement, setStateValue } = component;
      const editor = parentElement.querySelector('.editor');
      const source = parentElement.querySelector('.source');
      const toolbar = parentElement.querySelector('.toolbar');
      const publish = (html) => {
        setStateValue('html', html.replaceAll(/<strong>/gi, '<b>').replaceAll(/<[/]strong>/gi, '</b>'));
      };
      const current = data.html ?? '';
      if (!editor.matches(':focus') && !source.matches(':focus') && editor.innerHTML !== current) {
        editor.innerHTML = current;
        source.value = current;
      }

      editor.oninput = () => {
        source.value = editor.innerHTML;
      };
      editor.onblur = () => publish(editor.innerHTML);
      editor.onpaste = (event) => {
        event.preventDefault();
        document.execCommand('insertText', false, event.clipboardData.getData('text/plain'));
      };

      toolbar.querySelector('select').onchange = (event) => {
        editor.focus();
        document.execCommand('formatBlock', false, event.target.value);
        publish(editor.innerHTML);
      };
      toolbar.querySelectorAll('[data-command]').forEach((button) => {
        button.onmousedown = (event) => event.preventDefault();
        button.onclick = () => {
          const command = button.dataset.command;
          let value = null;
          if (command === 'createLink') {
            value = window.prompt('Adres linku (https://...)');
            if (!value) return;
            try {
              const url = new URL(value);
              if (!['http:', 'https:'].includes(url.protocol)) throw new Error();
              value = url.href;
            } catch {
              window.alert('Podaj poprawny adres zaczynający się od http:// lub https://');
              return;
            }
          }
          editor.focus();
          document.execCommand(command, false, value);
          publish(editor.innerHTML);
        };
      });
      toolbar.querySelector('.source-toggle').onclick = () => {
        const showSource = !parentElement.host.classList.contains('show-source');
        parentElement.host.classList.toggle('show-source', showSource);
        if (showSource) {
          source.value = editor.innerHTML;
          source.focus();
        } else {
          editor.innerHTML = source.value;
          editor.focus();
          publish(source.value);
        }
      };
      source.oninput = () => {
        editor.innerHTML = source.value;
      };
      source.onblur = () => {
        editor.innerHTML = source.value;
        publish(source.value);
      };
    }
    """,
)


def visual_html_editor(value: str, *, key: str, on_change=None) -> str:
    state = st.session_state.get(key, {})
    current = state.get("html", value)
    result = _visual_editor(
        data={"html": current},
        default={"html": current},
        key=key,
        on_html_change=on_change or (lambda: None),
    )
    return result.html if result.html is not None else current
