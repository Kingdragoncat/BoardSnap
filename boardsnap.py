#!/usr/bin/env python3
"""
BoardSnap (boardsnap.py) - turn any KiCad .kicad_pcb into a clean, captioned PNG (and timelapse GIFs).

Reads the board file directly (KiCad 6 - 10 s-expression format). No KiCad install needed.
Needs: python 3.9+, matplotlib (and pillow for GIFs).

  python boardsnap.py MyBoard.kicad_pcb                       # PNG next to the board
  python boardsnap.py MyBoard.kicad_pcb -c config.toml        # use your config
  python boardsnap.py MyBoard.kicad_pcb --caption "routed!" --step 7 -o step07.png
  python boardsnap.py MyBoard.kicad_pcb --ask                 # type the title / caption when it runs
  python boardsnap.py --frames -c config.toml                 # every [[frames]] entry + optional GIF
"""
import argparse, math, os, re, sys, json

# ----------------------------------------------------------------------------- config
DEFAULTS = {
    'project': {'name': 'My Board', 'version': '', 'author': ''},
    'text': {
        'title': '{name} {version}  ·  step {step}',
        'caption': '',
        'corner': '{routed} / {nets} nets routed',
        'footer': '',
        'legend': True,
        'font': 'monospace',
    },
    'render': {
        'width_in': 16.0, 'height_in': 12.4, 'dpi': 110, 'margin_mm': 3.0,
        'layers': ['B.Cu', 'F.Cu'],            # drawn bottom -> top
        'zones': 'filled',                      # filled | outline | none
        'inner_zones': True,                    # show In1/In2... zone outlines (hatched)
        'silkscreen': True, 'courtyards': False, 'references': False,
        'airwires': True,                       # yellow dashed lines for unrouted connections
    },
    'theme': {'preset': 'night'},
    'gif': {'output': '', 'ms_per_frame': 1200, 'hold_last_ms': 3000},
    'frames': [],
}
THEMES = {
    'night':   dict(background='#101418', board='#0f3d24', edge='#e0c040', pad='#d9b44a', via='#cfcfcf', silk='#e8e8e8',
                    fcu='#d8343a', bcu='#3b8fe0', inner=['#c08a20', '#6a4bc4', '#2a9d8f', '#e76f51'], airwire='#f5e663',
                    title='#e0c040', text='#f0f0f0', muted='#9aa9a9', courtyard='#7fb58a'),
    'kicad':   dict(background='#001023', board='#001023', edge='#d0d2cd', pad='#c2c200', via='#c2c2c2', silk='#f2eda1',
                    fcu='#c83434', bcu='#4d7fc4', inner=['#7fc87f', '#ce7d2c', '#4fcbcb', '#db628b'], airwire='#00f8ff',
                    title='#f2eda1', text='#e0e0e0', muted='#8a93a0', courtyard='#ff26e2'),
    'oshpark': dict(background='#1a1024', board='#3b1d5a', edge='#d4af37', pad='#e6c35c', via='#e6c35c', silk='#ffffff',
                    fcu='#9b59d0', bcu='#6c3fa0', inner=['#b089d6', '#8e6bbd', '#c9a0dc', '#7d5ba6'], airwire='#ffd166',
                    title='#e6c35c', text='#f5eefc', muted='#b7a6c9', courtyard='#b089d6'),
    'paper':   dict(background='#fbfaf6', board='#e9efe6', edge='#222222', pad='#b8860b', via='#555555', silk='#333333',
                    fcu='#c0392b', bcu='#2c6fbb', inner=['#d4a017', '#8e44ad', '#16a085', '#d35400'], airwire='#e67e22',
                    title='#222222', text='#222222', muted='#666666', courtyard='#27ae60'),
}

def deep_merge(a, b):
    out = dict(a)
    for k, v in b.items():
        out[k] = deep_merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
    return out

def load_config(path):
    cfg = json.loads(json.dumps(DEFAULTS))
    if not path: return cfg
    with open(path, 'rb') as f:
        if path.endswith('.json'): user = json.load(f)
        else:
            try: import tomllib
            except ImportError:
                try: import tomli as tomllib
                except ImportError: sys.exit('TOML config needs python 3.11+ (or `pip install tomli`), or use a .json config')
            user = tomllib.load(f)
    return deep_merge(cfg, user)

# ----------------------------------------------------------------------------- s-expression parser
def parse_sexp(text):
    tok = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')
    stack, cur, pos = [], [], 0
    n = len(text)
    while pos < n:
        m = tok.match(text, pos)
        if not m:
            if text[pos:].strip() == '': break
            raise ValueError(f'parse error near {text[pos:pos + 40]!r}')
        pos = m.end()
        if m.group(1): stack.append(cur); cur = []
        elif m.group(2): done = cur; cur = stack.pop(); cur.append(done)
        elif m.group(3) is not None: cur.append(Str(m.group(3).replace('\\"', '"').replace('\\\\', '\\')))
        elif m.group(4) is not None: cur.append(m.group(4))
    return cur[0]

class Str(str): pass          # quoted string (vs bare symbol)

def kids(node, key): return [e for e in node if isinstance(e, list) and e and e[0] == key]
def kid(node, key):
    for e in node:
        if isinstance(e, list) and e and e[0] == key: return e
    return None
def num(v): return float(v)
def xy(node): return (num(node[1]), num(node[2]))

# ----------------------------------------------------------------------------- board model
class Board:
    def __init__(self, path):
        self.path = path
        root = parse_sexp(open(path, encoding='utf-8').read())
        if root[0] != 'kicad_pcb': raise ValueError('not a .kicad_pcb file')
        self.edges, self.pads, self.tracks, self.vias, self.zones, self.silk, self.texts, self.court = [], [], [], [], [], [], [], []
        self.title = ''
        tb = kid(root, 'title_block')
        if tb is not None and kid(tb, 'title') is not None: self.title = str(kid(tb, 'title')[1])
        for e in root:
            if not isinstance(e, list) or not e: continue
            k = e[0]
            if k in ('gr_line', 'gr_arc', 'gr_circle', 'gr_rect', 'gr_poly'): self._graphic(e, None)
            elif k == 'gr_text': self._text(e, None)
            elif k == 'segment':
                self.tracks.append(dict(net=self._net(e), layer=str(kid(e, 'layer')[1]), w=num(kid(e, 'width')[1]),
                                        pts=[xy(kid(e, 'start')), xy(kid(e, 'end'))]))
            elif k == 'arc':
                s, m_, en = xy(kid(e, 'start')), xy(kid(e, 'mid')), xy(kid(e, 'end'))
                self.tracks.append(dict(net=self._net(e), layer=str(kid(e, 'layer')[1]), w=num(kid(e, 'width')[1]), pts=arc_pts(s, m_, en)))
            elif k == 'via':
                self.vias.append(dict(net=self._net(e), at=xy(kid(e, 'at')), d=num(kid(e, 'size')[1])))
            elif k == 'zone': self._zone(e)
            elif k == 'footprint' or k == 'module': self._footprint(e)
        self.nets = sorted({p['net'] for p in self.pads if p['net']})

    def _net(self, e):
        n = kid(e, 'net')
        if n is None: return None
        v = n[-1] if len(n) > 1 else None
        return str(v) if v is not None and not (isinstance(v, str) and not isinstance(v, Str) and v.isdigit() and len(n) == 2) else (str(n[2]) if len(n) > 2 else str(v))

    def _graphic(self, e, tf):
        layer = kid(e, 'layer'); layer = str(layer[1]) if layer else ''
        w = kid(e, 'width') or (kid(kid(e, 'stroke'), 'width') if kid(e, 'stroke') else None)
        w = num(w[1]) if w else 0.12
        k = e[0][3:] if e[0].startswith(('gr_', 'fp_')) else e[0]
        pts = None; closed = False
        if k == 'line': pts = [xy(kid(e, 'start')), xy(kid(e, 'end'))]
        elif k == 'arc':
            if kid(e, 'mid') is not None: pts = arc_pts(xy(kid(e, 'start')), xy(kid(e, 'mid')), xy(kid(e, 'end')))
            else:   # legacy: start = centre, end = start point, angle
                c, s = xy(kid(e, 'start')), xy(kid(e, 'end')); a = math.radians(num(kid(e, 'angle')[1]))
                r = math.dist(c, s); a0 = math.atan2(s[1] - c[1], s[0] - c[0])
                pts = [(c[0] + r * math.cos(a0 + a * i / 24), c[1] + r * math.sin(a0 + a * i / 24)) for i in range(25)]
        elif k == 'circle':
            c = xy(kid(e, 'center')); r = math.dist(c, xy(kid(e, 'end')))
            pts = [(c[0] + r * math.cos(2 * math.pi * i / 48), c[1] + r * math.sin(2 * math.pi * i / 48)) for i in range(49)]; closed = True
        elif k == 'rect':
            (x0, y0), (x1, y1) = xy(kid(e, 'start')), xy(kid(e, 'end'))
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]; closed = True
        elif k == 'poly':
            pts = [xy(p) for p in kids(kid(e, 'pts'), 'xy')]; pts.append(pts[0]); closed = True
        if not pts: return
        if tf: pts = [tf(p) for p in pts]
        item = dict(pts=pts, w=w, layer=layer, closed=closed)
        if layer == 'Edge.Cuts': self.edges.append(item)
        elif layer in ('F.SilkS', 'B.SilkS'): self.silk.append(item)
        elif layer in ('F.CrtYd', 'B.CrtYd'): self.court.append(item)

    def _text(self, e, tf, rot0=0.0):
        layer = kid(e, 'layer')
        if layer is None or str(layer[1]) not in ('F.SilkS', 'B.SilkS'): return
        if kid(e, 'hide') is not None or 'hide' in e: return
        eff = kid(e, 'effects')
        if eff is not None and (kid(eff, 'hide') is not None and str(kid(eff, 'hide')[-1]) != 'no'): return
        at = kid(e, 'at'); p = xy(at); ang = num(at[3]) if len(at) > 3 else 0.0
        size = 1.0
        if eff is not None and kid(eff, 'font') is not None and kid(kid(eff, 'font'), 'size') is not None: size = num(kid(kid(eff, 'font'), 'size')[1])
        just = ''
        if eff is not None and kid(eff, 'justify') is not None: just = ' '.join(str(v) for v in kid(eff, 'justify')[1:])
        txt = str(e[1]) if e[0] == 'gr_text' else str(e[2])
        if tf: p = tf(p)
        self.texts.append(dict(t=txt, at=p, ang=ang, size=size, just=just, layer=str(layer[1])))

    def _zone(self, e):
        net = self._net(e)
        layers = [str(kid(e, 'layer')[1])] if kid(e, 'layer') is not None else [str(v) for v in (kid(e, 'layers') or [])[1:]]
        keep = kid(e, 'keepout') is not None
        outline = [xy(p) for p in kids(kid(kid(e, 'polygon'), 'pts'), 'xy')] if kid(e, 'polygon') is not None else []
        fills = []
        for fp in kids(e, 'filled_polygon'):
            ly = kid(fp, 'layer'); fills.append((str(ly[1]) if ly else layers[0], [xy(p) for p in kids(kid(fp, 'pts'), 'xy')]))
        name = kid(e, 'name')
        self.zones.append(dict(net=net, layers=layers, keepout=keep, outline=outline, fills=fills, name=str(name[1]) if name else ''))

    def _footprint(self, e):
        at = kid(e, 'at'); fx, fy = xy(at); fr = num(at[3]) if len(at) > 3 else 0.0
        t = math.radians(fr); c, s = math.cos(t), math.sin(t)
        tf = lambda p: (fx + p[0] * c + p[1] * s, fy - p[0] * s + p[1] * c)
        ref = ''
        for g in e:
            if not isinstance(g, list) or not g: continue
            k = g[0]
            if k in ('fp_line', 'fp_arc', 'fp_circle', 'fp_rect', 'fp_poly'): self._graphic(g, tf)
            elif k == 'property' and str(g[1]) == 'Reference':
                ref = str(g[2])
                if kid(g, 'at') is not None: self._text(['fp_text', 'reference', ref] + g[3:], tf)
            elif k == 'fp_text' and str(g[1]) in ('reference', 'user'):
                if str(g[1]) == 'reference': ref = str(g[2])
                if '${' not in str(g[2]): self._text(g, tf)
            elif k == 'pad': self._pad(g, tf, fr, ref)

    def _pad(self, g, tf, fr, ref):
        typ, shape = str(g[2]), str(g[3])
        at = kid(g, 'at'); p = xy(at); ang = num(at[3]) if len(at) > 3 else fr
        sz = kid(g, 'size'); w, h = (num(sz[1]), num(sz[2])) if sz else (0.5, 0.5)
        lays = [str(v) for v in (kid(g, 'layers') or [])[1:]]
        cu = set()
        for l in lays:
            if l in ('*.Cu', 'F&B.Cu'): cu |= {'F.Cu', 'B.Cu'}
            elif l.endswith('.Cu'): cu.add(l)
        drill = kid(g, 'drill'); dr = 0.0
        if drill is not None:
            vals = [v for v in drill[1:] if not isinstance(v, list)]
            dr = num(vals[1]) if vals and vals[0] == 'oval' else (num(vals[0]) if vals else 0)
        centre = tf(p)
        prims = []
        if shape == 'custom' and kid(g, 'primitives') is not None:
            for pr in kids(kid(g, 'primitives'), 'gr_poly'):
                prims.append([xy(q) for q in kids(kid(pr, 'pts'), 'xy')])
        self.pads.append(dict(ref=ref, num=str(g[1]), type=typ, shape=shape, at=centre, ang=ang, w=w, h=h, cu=cu, drill=dr,
                              net=self._net(g), prims=prims))

def arc_pts(s, m, e, n=24):
    ax, ay = s; bx, by = m; cx, cy = e
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12: return [s, e]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.dist((ux, uy), s)
    a0 = math.atan2(ay - uy, ax - ux); a1 = math.atan2(by - uy, bx - ux); a2 = math.atan2(cy - uy, cx - ux)
    def norm(a): return a % (2 * math.pi)
    sweep = norm(a2 - a0)
    if norm(a1 - a0) > sweep: sweep -= 2 * math.pi
    return [(ux + r * math.cos(a0 + sweep * i / n), uy + r * math.sin(a0 + sweep * i / n)) for i in range(n + 1)]

def pad_poly(p):
    """pad outline as a list of points (world coords)"""
    x, y = p['at']; t = math.radians(p['ang']); c, s = math.cos(t), math.sin(t)
    rot = lambda q: (x + q[0] * c + q[1] * s, y - q[0] * s + q[1] * c)
    w, h = p['w'], p['h']
    if p['shape'] == 'custom' and p['prims']:
        return [[rot(q) for q in poly] for poly in p['prims']]
    if p['shape'] == 'circle' or (p['shape'] == 'oval' and abs(w - h) < 1e-6):
        r = w / 2; return [[(x + r * math.cos(2 * math.pi * i / 32), y + r * math.sin(2 * math.pi * i / 32)) for i in range(32)]]
    if p['shape'] == 'oval':
        r = min(w, h) / 2; L = (max(w, h) - min(w, h)) / 2; pts = []
        if w >= h:
            pts += [(L + r * math.cos(a), r * math.sin(a)) for a in [(-math.pi / 2 + math.pi * i / 12) for i in range(13)]]
            pts += [(-L + r * math.cos(a), r * math.sin(a)) for a in [(math.pi / 2 + math.pi * i / 12) for i in range(13)]]
        else:
            pts += [(r * math.cos(a), L + r * math.sin(a)) for a in [(math.pi * i / 12) for i in range(13)]]
            pts += [(r * math.cos(a), -L + r * math.sin(a)) for a in [(math.pi + math.pi * i / 12) for i in range(13)]]
        return [[rot(q) for q in pts]]
    return [[rot(q) for q in [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]]]

# ----------------------------------------------------------------------------- connectivity (for airwires + stats)
def point_in_poly(pt, poly):
    x, y = pt; inside = False; j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]; xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi: inside = not inside
        j = i
    return inside

def connectivity(b):
    items = []   # (net, layers, kind, data)
    for p in b.pads:
        if p['net']:
            polys = pad_poly(p); xs = [q[0] for pl in polys for q in pl]; ys = [q[1] for pl in polys for q in pl]
            items.append((p['net'], p['cu'], 'pad', (min(xs), min(ys), max(xs), max(ys)), p))
    for t in b.tracks:
        if t['net']:
            for a, c in zip(t['pts'], t['pts'][1:]): items.append((t['net'], {t['layer']}, 'seg', (a, c, t['w']), None))
    for v in b.vias:
        if v['net']: items.append((v['net'], {'*'}, 'via', (v['at'], v['d'] / 2), None))
    by = {}
    for i, it in enumerate(items): by.setdefault(it[0], []).append(i)
    zones = [z for z in b.zones if z['net'] and not z['keepout']]
    def pts(it):
        if it[2] == 'pad': x0, y0, x1, y1 = it[3]; return [((x0 + x1) / 2, (y0 + y1) / 2)]
        if it[2] == 'seg': return [it[3][0], it[3][1]]
        return [it[3][0]]
    def lay_ok(A, B): return '*' in A[1] or '*' in B[1] or bool(A[1] & B[1])
    def touch(A, B):
        if not lay_ok(A, B): return False
        for X, Y in ((A, B), (B, A)):
            if X[2] == 'pad':
                x0, y0, x1, y1 = X[3]
                if any(x0 - .01 <= px <= x1 + .01 and y0 - .01 <= py <= y1 + .01 for px, py in pts(Y)): return True
            if X[2] == 'via':
                (vx, vy), r = X[3]
                if any(math.hypot(px - vx, py - vy) <= r + .01 for px, py in pts(Y)): return True
            if X[2] == 'seg':
                (ax, ay), (bx, by_), w = X[3]; L2 = (bx - ax) ** 2 + (by_ - ay) ** 2
                for px, py in pts(Y):
                    u = 0 if L2 == 0 else max(0, min(1, ((px - ax) * (bx - ax) + (py - ay) * (by_ - ay)) / L2))
                    if math.hypot(px - ax - u * (bx - ax), py - ay - u * (by_ - ay)) <= w / 2 + .01: return True
        return False
    unrouted = {}
    for net, idx in by.items():
        par = {i: i for i in idx}
        def f(a):
            while par[a] != a: par[a] = par[par[a]]; a = par[a]
            return a
        for z in zones:
            if z['net'] != net: continue
            polys = [(ly, z['outline']) for ly in z['layers']]      # outline, not fill: fills have thermal holes around their own vias
            for ly, pl in polys:
                hit = [i for i in idx if (ly in items[i][1] or '*' in items[i][1]) and any(point_in_poly(q, pl) for q in pts(items[i]))]
                for a, c in zip(hit, hit[1:]): par[f(a)] = f(c)
        for a in range(len(idx)):
            for c in range(a + 1, len(idx)):
                i, j = idx[a], idx[c]
                if f(i) != f(j) and touch(items[i], items[j]): par[f(i)] = f(j)
        groups = {}
        for i in idx:
            if items[i][2] == 'pad': groups.setdefault(f(i), []).append(items[i][4])
        if len(groups) > 1: unrouted[net] = sorted(groups.values(), key=len, reverse=True)
    return unrouted

# ----------------------------------------------------------------------------- drawing
def render(board, cfg, out, step=None, caption=None):
    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon as MPoly
    th = dict(THEMES.get(cfg['theme'].get('preset', 'night'), THEMES['night'])); th.update({k: v for k, v in cfg['theme'].items() if k != 'preset'})
    R, T = cfg['render'], cfg['text']
    fig = plt.figure(figsize=(R['width_in'], R['height_in']), dpi=R['dpi']); fig.patch.set_facecolor(th['background'])
    top = 0.86 if (T['title'] or T['caption']) else 0.96
    ax = fig.add_axes([0.02, 0.04 if T['footer'] else 0.02, 0.96, top - (0.04 if T['footer'] else 0.02)]); ax.set_facecolor(th['background'])
    # board outline (fill = chained Edge.Cuts)
    allx = [q[0] for e in board.edges for q in e['pts']] or [p['at'][0] for p in board.pads]
    ally = [q[1] for e in board.edges for q in e['pts']] or [p['at'][1] for p in board.pads]
    x0, x1, y0, y1 = min(allx), max(allx), min(ally), max(ally)
    outline = chain(board.edges)
    if outline: ax.add_patch(MPoly(outline, closed=True, fc=th['board'], ec='none', zorder=0))
    else: ax.add_patch(MPoly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], closed=True, fc=th['board'], ec='none', zorder=0))
    # zones
    inner_i = 0; inner_col = {}
    for z in board.zones:
        if z['keepout']:
            if z['outline']: ax.add_patch(MPoly(z['outline'], closed=True, fc='none', ec='#ff66cc', lw=.8, ls='--', zorder=1))
            continue
        for ly in z['layers']:
            if ly in ('F.Cu', 'B.Cu'):
                if ly not in R['layers'] or R['zones'] == 'none': continue
                col = th['fcu'] if ly == 'F.Cu' else th['bcu']
                if R['zones'] == 'filled' and z['fills']:
                    for fl, pl in z['fills']:
                        if fl == ly and len(pl) > 2: ax.add_patch(MPoly(pl, closed=True, fc=col, alpha=.18, ec='none', zorder=1))
                elif z['outline']: ax.add_patch(MPoly(z['outline'], closed=True, fc=col, alpha=.12, ec=col, lw=.6, zorder=1))
            elif R['inner_zones'] and z['outline'] and ly.startswith('In'):
                if ly not in inner_col: inner_col[ly] = th['inner'][inner_i % len(th['inner'])]; inner_i += 1
                # only show islands, not full-board planes (those would just tint everything)
                zx = [q[0] for q in z['outline']]; zy = [q[1] for q in z['outline']]
                if (max(zx) - min(zx)) * (max(zy) - min(zy)) > 0.85 * (x1 - x0) * (y1 - y0): continue
                c = inner_col[ly]
                ax.add_patch(MPoly(z['outline'], closed=True, fc=c, alpha=.22, ec=c, lw=1.1, hatch='//', zorder=1))
                ax.text(sum(zx) / len(zx), max(zy) - 1, f'{ly}: {z["net"]}', color=c, fontsize=8, ha='center', va='bottom', zorder=6, family=T['font'])
    # tracks (bottom layer first)
    for ly in R['layers']:
        col = th['fcu'] if ly == 'F.Cu' else th['bcu'] if ly == 'B.Cu' else th['inner'][0]
        for t in board.tracks:
            if t['layer'] != ly: continue
            xs = [q[0] for q in t['pts']]; ys = [q[1] for q in t['pts']]
            ax.plot(xs, ys, color=col, lw=max(t['w'] * 72 / 25.4 * fig_scale(fig, ax), .5), solid_capstyle='round', alpha=.92, zorder=3)
    # pads
    for p in board.pads:
        if not (p['cu'] & set(R['layers'])) and p['type'] != 'np_thru_hole': continue
        for pl in pad_poly(p): ax.add_patch(MPoly(pl, closed=True, fc=th['pad'], ec='none', zorder=4))
        if p['drill'] > 0: ax.add_patch(plt.Circle(p['at'], p['drill'] / 2, fc=th['background'], ec='none', zorder=4.5))
    for v in board.vias:
        ax.add_patch(plt.Circle(v['at'], v['d'] / 2, fc=th['via'], ec='none', zorder=5))
        ax.add_patch(plt.Circle(v['at'], v['d'] / 5, fc=th['background'], ec='none', zorder=5.1))
    # silkscreen / courtyards / text
    if R['silkscreen']:
        for s in board.silk:
            if s['layer'] == 'F.SilkS': ax.plot([q[0] for q in s['pts']], [q[1] for q in s['pts']], color=th['silk'], lw=.5, alpha=.8, zorder=6)
        for t in board.texts:
            if t['layer'] != 'F.SilkS': continue
            ha = 'left' if 'left' in t['just'] else 'right' if 'right' in t['just'] else 'center'
            ax.text(t['at'][0], t['at'][1], t['t'], rotation=t['ang'], color=th['silk'], fontsize=max(4, t['size'] * 6.5 * fig_scale(fig, ax)),
                    ha=ha, va='center', family=T['font'], zorder=6, clip_on=True)
    if R['courtyards']:
        for s in board.court: ax.plot([q[0] for q in s['pts']], [q[1] for q in s['pts']], color=th['courtyard'], lw=.4, zorder=2)
    for e in board.edges: ax.plot([q[0] for q in e['pts']], [q[1] for q in e['pts']], color=th['edge'], lw=1.6, zorder=7)
    # airwires + stats
    unrouted = connectivity(board) if (R['airwires'] or '{routed}' in (T['corner'] + T['title'] + T['caption'] + T['footer'])) else {}
    if R['airwires']:
        for net, groups in unrouted.items():
            main = groups[0]
            for g in groups[1:]:
                a = g[0]; m = min(main, key=lambda q: math.dist(q['at'], a['at']))
                ax.plot([a['at'][0], m['at'][0]], [a['at'][1], m['at'][1]], color=th['airwire'], lw=.8, ls=(0, (3, 2)), zorder=8)
    M = R['margin_mm']
    ax.set_xlim(x0 - M, x1 + M); ax.set_ylim(y1 + M, y0 - M); ax.set_aspect('equal'); ax.axis('off')
    P = cfg['project']
    vars_ = dict(name=P.get('name') or board.title or os.path.splitext(os.path.basename(board.path))[0], version=P.get('version', ''), author=P.get('author', ''),
                 step=f'{step:02d}' if isinstance(step, int) else (step or ''), nets=len(board.nets), routed=len(board.nets) - len(unrouted),
                 unrouted=len(unrouted), airwires=sum(len(g) - 1 for g in unrouted.values()), tracks=len(board.tracks), vias=len(board.vias),
                 parts=len({p['ref'] for p in board.pads}), file=os.path.basename(board.path))
    fmt = lambda s: ' '.join(s.format(**vars_).split()) if s else ''
    title = fmt(T['title']) if step is not None or '{step}' not in T['title'] else fmt(T['title'].replace('·  step {step}', '').replace('step {step}', ''))
    cap = fmt(caption if caption is not None else T['caption'])
    if title: fig.text(0.03, 0.95, title, color=th['title'], fontsize=20, weight='bold', family=T['font'])
    if cap: fig.text(0.03, 0.915, cap, color=th['text'], fontsize=14, family=T['font'])
    if T['corner']: fig.text(0.97, 0.95, fmt(T['corner']), color=th['muted'], fontsize=11, ha='right', family=T['font'])
    if T['legend']:
        fig.text(0.97, 0.915, 'F.Cu red · B.Cu blue · vias grey' + (' · airwires dashed' if R['airwires'] else ''), color=th['muted'], fontsize=9, ha='right', family=T['font'])
    if T['footer']: fig.text(0.03, 0.012, fmt(T['footer']), color=th['muted'], fontsize=10, family=T['font'])
    fig.savefig(out, facecolor=th['background']); plt.close(fig)
    return out, vars_

def fig_scale(fig, ax):
    """points per mm on the axes, relative to a 210 mm wide board on a 16 inch figure (keeps widths sane)"""
    x0, x1 = ax.get_xlim()
    span = abs(x1 - x0) if abs(x1 - x0) > 1 else 216
    return (fig.get_figwidth() * 0.96 * 25.4 / span) / (16 * 0.96 * 25.4 / 216) * 0.9

def chain(edges, tol=0.05):
    segs = [e['pts'] for e in edges if not e['closed']]
    closed = [e['pts'] for e in edges if e['closed']]
    if not segs:
        return max(closed, key=lambda pl: (max(q[0] for q in pl) - min(q[0] for q in pl)) * (max(q[1] for q in pl) - min(q[1] for q in pl))) if closed else None
    path = list(segs.pop(0))
    while segs:
        end = path[-1]; found = False
        for i, s in enumerate(segs):
            if math.dist(s[0], end) < tol: path += s[1:]; segs.pop(i); found = True; break
            if math.dist(s[-1], end) < tol: path += s[::-1][1:]; segs.pop(i); found = True; break
        if not found: break
    return path if math.dist(path[0], path[-1]) < tol else None

# ----------------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description='BoardSnap: render a KiCad .kicad_pcb to a captioned PNG (and timelapse GIFs).')
    ap.add_argument('board', nargs='?', help='.kicad_pcb file')
    ap.add_argument('-c', '--config', help='config .toml or .json')
    ap.add_argument('-o', '--out', help='output .png')
    ap.add_argument('--title', help='title template, e.g. "{name} v2"')
    ap.add_argument('--caption', help='caption under the title')
    ap.add_argument('--step', type=int, help='step number (fills {step})')
    ap.add_argument('--theme', choices=sorted(THEMES), help='colour preset')
    ap.add_argument('--ask', action='store_true', help='prompt for the title, caption and step')
    ap.add_argument('--frames', action='store_true', help='render every [[frames]] entry in the config')
    ap.add_argument('--no-airwires', action='store_true')
    a = ap.parse_args()
    cfg = load_config(a.config)
    if a.theme: cfg['theme']['preset'] = a.theme
    if a.title: cfg['text']['title'] = a.title
    if a.no_airwires: cfg['render']['airwires'] = False
    if a.frames:
        frames = cfg.get('frames') or []
        if not frames: sys.exit('no [[frames]] in the config')
        base = os.path.dirname(os.path.abspath(a.config)) if a.config else os.getcwd()
        outs = []
        for i, fr in enumerate(frames, 1):
            path = os.path.join(base, fr['file']); step = fr.get('step', i)
            out = os.path.join(base, fr.get('out') or f'step{step:02d}.png')
            render(Board(path), deep_merge(cfg, fr.get('override', {})), out, step=step, caption=fr.get('caption'))
            print('wrote', out); outs.append(out)
        g = cfg.get('gif', {})
        if g.get('output'): make_gif(outs, os.path.join(base, g['output']), g.get('ms_per_frame', 1200), g.get('hold_last_ms', 3000))
        return
    if not a.board: ap.error('give a .kicad_pcb (or --frames with a config)')
    cap, step = a.caption, a.step
    if a.ask:
        t = input(f'title [{cfg["text"]["title"]}]: ').strip()
        if t: cfg['text']['title'] = t
        c = input('caption: ').strip(); cap = c or cap
        s = input('step number (blank = none): ').strip(); step = int(s) if s.isdigit() else step
    out = a.out or os.path.splitext(a.board)[0] + (f'_step{step:02d}' if step is not None else '_snap') + '.png'
    _, v = render(Board(a.board), cfg, out, step=step, caption=cap)
    print(f'wrote {out}  ({v["routed"]}/{v["nets"]} nets routed, {v["tracks"]} tracks, {v["vias"]} vias)')

def make_gif(pngs, out, ms, hold):
    try: from PIL import Image
    except ImportError: print('GIF needs pillow: pip install pillow'); return
    ims = [Image.open(p).convert('RGB') for p in pngs]
    durs = [ms] * (len(ims) - 1) + [hold]
    ims[0].save(out, save_all=True, append_images=ims[1:], duration=durs, loop=0, optimize=True)
    print('wrote', out)

if __name__ == '__main__':
    main()
