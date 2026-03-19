import re

with open('static/index.html', encoding='utf-8') as f:
    html = f.read()

def replace_radius_in_class(html, class_name, new_radius):
    # 인라인 (한줄) + 멀티라인 CSS 블록에서 해당 클래스의 border-radius만 교체
    escaped = re.escape(class_name)
    pattern = escaped + r'(\s*\{[^}]*?)border-radius:\s*[\d.]+px'
    return re.sub(pattern, lambda m: m.group(0).rsplit('border-radius:', 1)[0] + f'border-radius: {new_radius}', html, flags=re.DOTALL)

small = [
    '.watch-pill', '.watch-alert-level', '.morning-pill', '.morning-goal-state',
    '.week-goal-delete', '.task-attn', '.ci-badge', '.ci-link',
    '.ops-mini-btn', '.ops-action-btn', '.mpb-btn',
    '.btn-cc', '.btn-view', '.filter-clear',
    '.grp-folder-view', '.grp-folder-btn',
    '.strategic-start-btn', '.banner-btn', '.btn-sync', '.btn-launch',
]
large = ['.btn-add', '.btn-save', '.btn-ci', '.comment-send-btn']

for cls in small:
    html = replace_radius_in_class(html, cls, '4px')
for cls in large:
    html = replace_radius_in_class(html, cls, '6px')

with open('static/index.html', 'w', encoding='utf-8') as f:
    f.write(html)

# verify
bad = []
for cls in small + large:
    escaped = re.escape(cls)
    pattern = escaped + r'\s*\{[^}]*border-radius:\s*(980|999)px'
    if re.search(pattern, html, re.DOTALL):
        bad.append(cls)

print('Still pill:', bad if bad else 'none')
print('OK')
