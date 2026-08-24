from __future__ import annotations

import streamlit as st

from description_output import sanitize_html


_component_func = None
if hasattr(st.components, "v2") and hasattr(st.components.v2, "component"):
    _component_func = st.components.v2.component(
        name="visual_html_editor",
        html="""
        <div class="toolbar" role="toolbar" aria-label="Formatowanie opisu">
          <select aria-label="Format akapitu" class="block-select">
            <option value="p">Akapit</option>
            <option value="h2">Nagłówek H2</option>
            <option value="h3">Nagłówek H3</option>
          </select>
          <button type="button" data-command="bold" aria-label="Pogrubienie" title="Pogrubienie"><b>B</b></button>
          <button type="button" data-command="createLink" aria-label="Dodaj link" title="Dodaj lub zmień link">🔗 Link</button>
          <button type="button" data-command="unlink" aria-label="Usuń link" title="Usuń link">Usuń link</button>
          <button type="button" data-command="undo" aria-label="Cofnij" title="Cofnij">↶</button>
          <button type="button" data-command="redo" aria-label="Ponów" title="Ponów">↷</button>
          <button type="button" class="source-toggle" aria-label="Przełącz widok HTML" title="Kod HTML">&lt;/&gt;</button>
        </div>
        <div class="editor" contenteditable="true" role="textbox" aria-multiline="true"></div>
        <textarea class="source" aria-label="Kod HTML opisu" spellcheck="false"></textarea>
        """,
        css="""
        :host { display: block; color: var(--st-text-color, #31333f); font-family: sans-serif; }
        .toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: .35rem; padding: .55rem; border: 1px solid var(--st-border-color, #e0e0e0); border-bottom: 0; border-radius: .5rem .5rem 0 0; background: var(--st-secondary-background-color, #f8f9fa); }
        button, select { min-height: 2.2rem; padding: .35rem .65rem; border: 1px solid var(--st-border-color, #d0d0d0); border-radius: .35rem; background: var(--st-background-color, #ffffff); color: var(--st-text-color, #31333f); cursor: pointer; font-size: 0.9rem; }
        button:hover, select:hover { border-color: #0b57d0; }
        .source-toggle { margin-left: auto; }
        .editor, .source { box-sizing: border-box; width: 100%; min-height: 22rem; padding: 1rem 1.25rem; border: 1px solid var(--st-border-color, #e0e0e0); border-radius: 0 0 .5rem .5rem; background: var(--st-background-color, #ffffff); color: var(--st-text-color, #31333f); font: inherit; line-height: 1.6; overflow: auto; }
        .editor:focus, .source:focus { outline: 2px solid #0b57d0; outline-offset: -2px; }
        .editor h2 { font-size: 1.35rem; font-weight: 700; margin: 1.25rem 0 .5rem; color: #1a1a1a; }
        .editor h3 { font-size: 1.15rem; font-weight: 600; margin: 1.1rem 0 .5rem; color: #2a2a2a; }
        .editor p { margin: .65rem 0; }
        .editor b, .editor strong { font-weight: 700; }
        .editor a { color: #0b57d0; text-decoration: underline; font-weight: 600; background: rgba(11, 87, 208, 0.08); padding: 1px 4px; border-radius: 3px; cursor: pointer; }
        .editor a:hover { background: rgba(11, 87, 208, 0.18); }
        .source { display: none; resize: vertical; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .9rem; }
        :host(.show-source) .editor { display: none; }
        :host(.show-source) .source { display: block; }
        """,
        js=r"""
        export default function(component) {
          const { data, parentElement, setStateValue } = component;
          const editor = parentElement.querySelector('.editor');
          const source = parentElement.querySelector('.source');
          const toolbar = parentElement.querySelector('.toolbar');
          const blockSelect = toolbar.querySelector('.block-select');

          const cleanHtml = (raw) => {
            let h = raw || '';
            h = h.replaceAll(/<strong>/gi, '<b>').replaceAll(/<[/]strong>/gi, '</b>');
            h = h.replaceAll(/<em>/gi, '<i>').replaceAll(/<[/]em>/gi, '</i>');
            for (let i = 0; i < 5; i++) {
              if (!h.toLowerCase().includes('<span')) break;
              h = h.replaceAll(/<span[^>]*>/gi, '').replaceAll(/<[/]span>/gi, '');
            }
            for (let i = 0; i < 3; i++) {
              if (!h.toLowerCase().includes('<div') && !h.toLowerCase().includes('<font')) break;
              h = h.replaceAll(/<div[^>]*>/gi, '<p>').replaceAll(/<[/]div>/gi, '</p>');
              h = h.replaceAll(/<font[^>]*>/gi, '').replaceAll(/<[/]font>/gi, '');
            }
            h = h.replace(/<([a-zA-Z0-9]+)\s+[^>]*>/gi, (match, tag) => {
              const low = tag.toLowerCase();
              if (low === 'a') {
                const hrefMatch = match.match(/href=(["'])(.*?)\1/i);
                return hrefMatch ? `<a href="${hrefMatch[2]}">` : '<a>';
              }
              return `<${low}>`;
            });
            return h.trim();
          };

          const publish = (html) => {
            const cleaned = cleanHtml(html);
            setStateValue('html', cleaned);
          };

          const current = data.html ?? '';
          if (!editor.matches(':focus') && !source.matches(':focus') && editor.innerHTML !== current) {
            editor.innerHTML = current;
            source.value = current;
          }

          const updateBlockSelect = () => {
            const sel = window.getSelection();
            if (!sel || !sel.rangeCount) return;
            let node = sel.anchorNode;
            if (node && node.nodeType === 3) node = node.parentNode;
            while (node && node !== editor && node !== parentElement) {
              const tag = (node.tagName || '').toLowerCase();
              if (['p', 'h2', 'h3'].includes(tag)) {
                blockSelect.value = tag;
                return;
              }
              node = node.parentNode;
            }
            blockSelect.value = 'p';
          };

          editor.oninput = () => {
            source.value = cleanHtml(editor.innerHTML);
          };
          editor.onblur = () => publish(editor.innerHTML);
          editor.onkeyup = updateBlockSelect;
          editor.onmouseup = updateBlockSelect;

          editor.onclick = (event) => {
            updateBlockSelect();
            const link = event.target.closest('a');
            if (link) {
              const currentUrl = link.getAttribute('href') || '';
              const newUrl = window.prompt('Edytuj adres URL linku (lub zostaw puste, aby usunąć):', currentUrl);
              if (newUrl !== null) {
                const trimmed = newUrl.trim();
                if (trimmed === '') {
                  link.replaceWith(document.createTextNode(link.textContent));
                } else {
                  try {
                    const u = new URL(trimmed);
                    link.setAttribute('href', u.href);
                  } catch {
                    window.alert('Podaj poprawny adres zaczynający się od http:// lub https://');
                    return;
                  }
                }
                source.value = cleanHtml(editor.innerHTML);
                publish(editor.innerHTML);
              }
            }
          };

          editor.onpaste = (event) => {
            event.preventDefault();
            const text = event.clipboardData.getData('text/plain');
            document.execCommand('insertText', false, text);
            source.value = cleanHtml(editor.innerHTML);
          };

          blockSelect.onchange = (event) => {
            editor.focus();
            document.execCommand('formatBlock', false, event.target.value);
            source.value = cleanHtml(editor.innerHTML);
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
                  const url = new URL(value.trim());
                  if (!['http:', 'https:'].includes(url.protocol)) throw new Error();
                  value = url.href;
                } catch {
                  window.alert('Podaj poprawny adres zaczynający się od http:// lub https://');
                  return;
                }
              }
              editor.focus();
              document.execCommand(command, false, value);
              source.value = cleanHtml(editor.innerHTML);
              publish(editor.innerHTML);
            };
          });

          toolbar.querySelector('.source-toggle').onclick = () => {
            const showSource = !parentElement.host.classList.contains('show-source');
            parentElement.host.classList.toggle('show-source', showSource);
            if (showSource) {
              source.value = cleanHtml(editor.innerHTML);
              source.focus();
            } else {
              editor.innerHTML = cleanHtml(source.value);
              editor.focus();
              publish(source.value);
            }
          };

          source.oninput = () => {
            editor.innerHTML = cleanHtml(source.value);
          };
          source.onblur = () => {
            const cleaned = cleanHtml(source.value);
            editor.innerHTML = cleaned;
            publish(cleaned);
          };
        }
        """,
    )


def _visual_editor(data=None, default=None, key=None, on_html_change=None):
    if _component_func is not None:
        return _component_func(data=data, default=default, key=key, on_html_change=on_html_change)
    return type("Result", (), {"html": (data or {}).get("html", "")})()


def visual_html_editor(value: str, *, key: str, on_change=None) -> str:
    cleaned_start = sanitize_html(value)
    state = st.session_state.get(key, {})
    current = state.get("html", cleaned_start)
    result = _visual_editor(
        data={"html": current},
        default={"html": current},
        key=key,
        on_html_change=on_change or (lambda: None),
    )
    res_val = result.html if result.html is not None else current
    return sanitize_html(res_val)

