"""Offline 3D packing planner with verifiable per-part coordinates.

Each order row is checked against real geometry exported by the desktop
packing software: every part carries its outer contour, an explicit list
of allowed 3D rotations, a cushion thickness, fragile faces and weight;
every box type carries inner dimensions, load capacity, a counted
inventory and handling minutes.  A plan is feasible only when every part
occupies its own cushioned volume (cushions may touch but never
interpenetrate), nothing rests on a fragile face that is marked as
non-load-bearing, and the box load capacity holds.  Feasible plans are
priced per physical box -- the larger of scale weight and volumetric
weight falls into a carrier price tier -- and the cheapest plan that
still makes the cutoff is selected; splitting into two boxes can win or
lose against one big box depending on tier boundaries.  When no layout
exists the diagnosis names the minimal combination of parts and the
binding orientation / fragile-face / load constraint instead of
reporting a bare "volume shortage".
"""
import json
from datetime import datetime, timedelta
from itertools import combinations
from .ingest import capture
from .store import connect, save, decide

EPS = 1e-9
MAX_PARTS = 6
MAX_BOX_TYPES = 2
SEARCH_BUDGET = 400000
FACES = ('+x', '-x', '+y', '-y', '+z', '-z')
CONTRACTS = ('single', 'multi')
ALL_PERMS = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))


class Spec(Exception):
    """Invalid order row; reason is a stable machine-readable code."""
    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_part(raw):
    pid = str(raw.get('id'))
    bad = Spec('part_spec_invalid', {'part': pid})
    size = raw.get('size')
    if not isinstance(size, list) or len(size) != 3:
        raise bad
    size = [_float(x) for x in size]
    weight = _float(raw.get('weight'))
    cushion = _float(raw.get('cushion', 0))
    if (any(x is None or x <= 0 for x in size) or weight is None or weight <= 0
            or cushion is None or cushion < 0):
        raise bad
    fragile = raw.get('fragile_faces', [])
    if not isinstance(fragile, list) or any(f not in FACES for f in fragile):
        raise bad
    rotations = raw.get('rotations')
    if not isinstance(rotations, list) or not rotations:
        raise Spec('rotations_missing', {'part': pid})
    rots = []
    for rot in rotations:
        if not isinstance(rot, dict):
            raise bad
        name, axes, up = rot.get('name'), rot.get('axes'), rot.get('up')
        if (not isinstance(name, str) or not name or not isinstance(axes, list)
                or sorted(axes) != [0, 1, 2] or up not in FACES):
            raise bad
        rots.append({'name': name, 'axes': list(axes), 'up': up})
    return {'id': pid, 'size': size, 'weight': weight, 'cushion': cushion,
            'fragile': set(fragile), 'rotations': rots}


def _parse_box(raw):
    btype = str(raw.get('type'))
    bad = Spec('box_spec_invalid', {'box_type': btype})
    inner = raw.get('inner')
    if not isinstance(inner, list) or len(inner) != 3:
        raise bad
    inner = [_float(x) for x in inner]
    max_weight = _float(raw.get('max_weight'))
    tare = _float(raw.get('tare', 0))
    handling = _float(raw.get('handling_minutes', 0))
    count = raw.get('count')
    if (any(x is None or x <= 0 for x in inner) or max_weight is None or max_weight <= 0
            or tare is None or tare < 0 or handling is None or handling < 0
            or not isinstance(count, int) or isinstance(count, bool) or count < 0):
        raise bad
    return {'type': btype, 'inner': inner, 'max_weight': max_weight, 'tare': tare,
            'handling': handling, 'count': count, 'certified': bool(raw.get('certified', False))}


def _parse_carrier(raw):
    bad = Spec('carrier_spec_invalid')
    if not isinstance(raw, dict):
        raise bad
    divisor = _float(raw.get('dim_divisor'))
    tolerance = _float(raw.get('weight_tolerance', 0.2))
    tiers = raw.get('tiers')
    if (divisor is None or divisor <= 0 or tolerance is None or tolerance < 0
            or not isinstance(tiers, list) or not tiers):
        raise bad
    parsed = []
    for tier in tiers:
        if not isinstance(tier, dict):
            raise bad
        up_to, price = _float(tier.get('up_to')), _float(tier.get('price'))
        if up_to is None or up_to <= 0 or price is None or price < 0:
            raise bad
        parsed.append({'up_to': up_to, 'price': price})
    parsed.sort(key=lambda t: t['up_to'])
    return {'dim_divisor': divisor, 'weight_tolerance': tolerance, 'tiers': parsed}


def parse_order(row):
    parts_raw, boxes_raw = row.get('parts'), row.get('boxes')
    if not parts_raw or not boxes_raw:
        raise Spec('parts_or_boxes_missing')
    contract = row.get('contract')
    if contract is None:
        raise Spec('contract_missing')
    if contract not in CONTRACTS:
        raise Spec('contract_unknown', {'contract': str(contract)})
    if len(parts_raw) > MAX_PARTS or len(boxes_raw) > MAX_BOX_TYPES:
        raise Spec('sample_limit_exceeded')
    parts = [_parse_part(p) for p in parts_raw]
    if len({p['id'] for p in parts}) != len(parts):
        raise Spec('duplicate_part_id')
    boxes = [_parse_box(b) for b in boxes_raw]
    if len({b['type'] for b in boxes}) != len(boxes):
        raise Spec('duplicate_box_type')
    carrier = _parse_carrier(row.get('carrier'))
    try:
        pack_start = datetime.fromisoformat(row['pack_start'])
        cutoff = datetime.fromisoformat(row['cutoff'])
        if cutoff <= pack_start:
            raise ValueError('cutoff_not_after_start')
    except (KeyError, TypeError, ValueError):
        raise Spec('schedule_missing')
    return {'contract': contract, 'parts': parts, 'boxes': boxes, 'carrier': carrier,
            'pack_start': pack_start, 'cutoff': cutoff}


def _orientations(part, relax_rotations=False):
    """Expand the explicitly listed rotations into world-space envelopes.

    Each orientation yields the cushioned outer dims and the part-local
    faces that point straight up / down, which is all the load checks
    need.  A mirror flip that turns a fragile face into a load-bearing
    one is rejected later by the fragile-face contact rule.
    """
    rots = part['rotations']
    if relax_rotations:
        rots = [{'name': 'any', 'axes': list(perm), 'up': '+z'} for perm in ALL_PERMS]
    seen, out = set(), []
    for rot in rots:
        dims = tuple(part['size'][rot['axes'][i]] + 2 * part['cushion'] for i in range(3))
        up = rot['up']
        key = (dims, up)
        if key in seen:
            continue
        seen.add(key)
        out.append({'name': rot['name'], 'dims': dims, 'top': up,
                    'bottom': ('-' if up[0] == '+' else '+') + up[1]})
    return out


def _volume(part):
    dims = [s + 2 * part['cushion'] for s in part['size']]
    return dims[0] * dims[1] * dims[2]


def _overlap(pos_a, dim_a, pos_b, dim_b):
    # Strict overlap: boxes (and cushions) touching at a face, edge or
    # corner are tangent, not interpenetrating -- a definite result.
    return all(pos_a[i] < pos_b[i] + dim_b[i] - EPS and
               pos_b[i] < pos_a[i] + dim_a[i] - EPS for i in range(3))


def _xy_shared(pos_a, dim_a, pos_b, dim_b):
    dx = min(pos_a[0] + dim_a[0], pos_b[0] + dim_b[0]) - max(pos_a[0], pos_b[0])
    dy = min(pos_a[1] + dim_a[1], pos_b[1] + dim_b[1]) - max(pos_a[1], pos_b[1])
    return dx > EPS and dy > EPS


def layout(parts, box, relax_fragile=False, relax_rotations=False):
    """Deterministic supported-placement search.

    Every part rests on the box floor or exactly on another part's
    cushion top; cushioned envelopes never overlap; nothing may rest on
    a fragile top face, and a part may not rest its own fragile bottom
    face on another part.  Returns per-part coordinates of the cushioned
    envelope (origin at the box's inner bottom corner) or None.
    """
    if sum(p['weight'] for p in parts) > box['max_weight'] + EPS:
        return None
    inner = box['inner']
    orientations = {p['id']: _orientations(p, relax_rotations) for p in parts}
    ordered = sorted(parts, key=lambda p: (-_volume(p), p['id']))
    placed = []
    budget = [SEARCH_BUDGET]

    def supported(pos, dims):
        if pos[2] <= EPS:
            return True
        return any(abs(q['pos'][2] + q['dims'][2] - pos[2]) <= EPS and
                   _xy_shared(pos, dims, q['pos'], q['dims']) for q in placed)

    def fragile_safe(part, orient, pos, dims):
        for q in placed:
            if (abs(q['pos'][2] + q['dims'][2] - pos[2]) <= EPS
                    and _xy_shared(pos, dims, q['pos'], q['dims'])):
                if q['orient']['top'] in q['part']['fragile']:
                    return False
                if orient['bottom'] in part['fragile']:
                    return False
        return True

    def backtrack(remaining):
        if not remaining:
            return True
        budget[0] -= 1
        if budget[0] <= 0:
            return False
        xs = {0.0} | {q['pos'][0] + q['dims'][0] for q in placed}
        ys = {0.0} | {q['pos'][1] + q['dims'][1] for q in placed}
        zs = {0.0} | {q['pos'][2] + q['dims'][2] for q in placed}
        positions = sorted((z, y, x) for z in zs for y in ys for x in xs)
        for part in remaining:
            for orient in orientations[part['id']]:
                dims = orient['dims']
                for z, y, x in positions:
                    pos = (x, y, z)
                    if any(pos[i] + dims[i] > inner[i] + EPS for i in range(3)):
                        continue
                    if any(_overlap(pos, dims, q['pos'], q['dims']) for q in placed):
                        continue
                    if not supported(pos, dims):
                        continue
                    if not relax_fragile and not fragile_safe(part, orient, pos, dims):
                        continue
                    placed.append({'part': part, 'orient': orient, 'pos': pos, 'dims': dims})
                    if backtrack([q for q in remaining if q is not part]):
                        return True
                    placed.pop()
        return False

    if not backtrack(ordered):
        return None
    return [{'part': q['part']['id'], 'orientation': q['orient']['name'],
             'position': [round(v, 6) for v in q['pos']],
             'occupied': [round(v, 6) for v in q['dims']],
             'weight': q['part']['weight']}
            for q in sorted(placed, key=lambda q: q['part']['id'])]


def _partitions(parts):
    """Unordered splits into two non-empty groups; parts[0] stays in group one."""
    n = len(parts)
    for mask in range(1, 2 ** n - 1):
        if not mask & 1:
            continue
        yield ([parts[i] for i in range(n) if mask >> i & 1],
               [parts[i] for i in range(n) if not mask >> i & 1])


def _price(box_type, gross, carrier):
    inner = box_type['inner']
    volumetric = inner[0] * inner[1] * inner[2] / 1000.0 / carrier['dim_divisor']
    chargeable = max(gross, volumetric)
    for tier in carrier['tiers']:
        if chargeable <= tier['up_to'] + EPS:
            return {'volumetric_weight': round(volumetric, 6),
                    'chargeable_weight': round(chargeable, 6),
                    'tier_up_to': tier['up_to'], 'price': tier['price']}
    return None


def _build_plan(spec, groups):
    boxes, counters = [], {}
    for box_type, placed in groups:
        counters[box_type['type']] = counters.get(box_type['type'], 0) + 1
        box_no = '%s#%d' % (box_type['type'], counters[box_type['type']])
        gross = box_type['tare'] + sum(p['weight'] for p in placed)
        priced = _price(box_type, gross, spec['carrier'])
        if priced is None:
            return None, 'rate_tier_exceeded'
        tolerance = spec['carrier']['weight_tolerance']
        boxes.append({'box_no': box_no, 'box_type': box_type['type'], 'parts': placed,
                      'gross_weight': round(gross, 6),
                      'weight_range': [round(gross - tolerance, 6), round(gross + tolerance, 6)],
                      'handling_minutes': box_type['handling'], **priced})
    ready_at = spec['pack_start'] + timedelta(minutes=sum(b['handling_minutes'] for b in boxes))
    return {'boxes': boxes, 'total_price': round(sum(b['price'] for b in boxes), 6),
            'ready_at': ready_at.isoformat(),
            'makes_cutoff': ready_at <= spec['cutoff']}, None


def _candidates(spec):
    parts = spec['parts']
    available = [b for b in spec['boxes'] if b['count'] >= 1]
    found = []
    if spec['contract'] == 'single':
        for box_type in available:
            if box_type['certified']:
                placed = layout(parts, box_type)
                if placed:
                    found.append([(box_type, placed)])
        return found
    for box_type in available:
        placed = layout(parts, box_type)
        if placed:
            found.append([(box_type, placed)])
    if len(parts) > 1:
        for first_type in available:
            for second_type in available:
                if first_type is second_type and first_type['count'] < 2:
                    continue
                for group_a, group_b in _partitions(parts):
                    placed_a = layout(group_a, first_type)
                    if not placed_a:
                        continue
                    placed_b = layout(group_b, second_type)
                    if placed_b:
                        found.append([(first_type, placed_a), (second_type, placed_b)])
    return found


def _part_cause(part, available):
    if all(part['weight'] > b['max_weight'] + EPS for b in available):
        cause = 'weight_capacity'
    elif any(layout([part], b, relax_rotations=True) for b in available):
        cause = 'orientation_restriction'
    else:
        cause = 'dimensions'
    return {'parts': [part['id']], 'cause': cause,
            'boxes': {b['type']: 'fits' if layout([part], b) else 'no_fit' for b in available}}


def _subset_cause(subset, box):
    """Why this combination cannot share this box: relax constraints one by one."""
    if sum(p['weight'] for p in subset) > box['max_weight'] + EPS:
        return 'weight_capacity'
    if layout(subset, box, relax_fragile=True, relax_rotations=True) is None:
        return 'dimensions'
    if layout(subset, box, relax_rotations=True) is None:
        return 'fragile_face'
    if layout(subset, box, relax_fragile=True) is None:
        return 'orientation_restriction'
    return 'fragile_face'


def _minimal_unpackable(parts, eligible):
    ordered = sorted(parts, key=lambda p: p['id'])
    for size in range(1, len(ordered) + 1):
        hits = [list(combo) for combo in combinations(ordered, size)
                if all(layout(list(combo), b) is None for b in eligible)]
        if hits:
            return hits[:3]
    return []


def _min_boxes(parts, available):
    ordered = sorted(parts, key=lambda p: p['id'])
    best = [len(ordered) + 1]

    def search(index, groups):
        if len(groups) >= best[0]:
            return
        if index == len(ordered):
            if all(any(layout(group, b) for b in available) for group in groups):
                best[0] = len(groups)
            return
        part = ordered[index]
        for group in groups:
            group.append(part)
            search(index + 1, groups)
            group.pop()
        search(index + 1, groups + [[part]])

    search(0, [])
    return best[0]


def _diagnose(spec, available):
    parts = spec['parts']
    if spec['contract'] == 'single':
        eligible = [b for b in available if b['certified']]
        if not eligible:
            return ('待协调', 'certified_box_unavailable',
                    {'causes': [{'cause': 'certified_box_unavailable',
                                 'certified_types': [b['type'] for b in spec['boxes'] if b['certified']],
                                 'available_types': [b['type'] for b in available]}]}, None)
        causes = [{'parts': [p['id'] for p in subset],
                   'boxes': {b['type']: _subset_cause(subset, b) for b in eligible}}
                  for subset in _minimal_unpackable(parts, eligible)]
        return '待协调', 'layout_infeasible', {'causes': causes}, None
    needed = _min_boxes(parts, available)
    if needed > 2:
        return ('待协调', 'box_count_exceeded',
                {'causes': [{'cause': 'needs_more_than_two_boxes', 'min_boxes': needed,
                             'parts': [p['id'] for p in parts]}]}, None)
    return ('待协调', 'box_inventory_insufficient',
            {'causes': [{'cause': 'box_inventory_insufficient',
                         'available': {b['type']: b['count'] for b in spec['boxes']}}]}, None)


def evaluate(spec):
    """Return (status, reason, detail, best_plan_or_None) for a parsed order."""
    available = [b for b in spec['boxes'] if b['count'] >= 1]
    if not available:
        return ('待协调', 'box_inventory_insufficient',
                {'causes': [{'cause': 'no_box_available'}]}, None)
    unfit = [_part_cause(part, available) for part in spec['parts']
             if not any(layout([part], b) for b in available)]
    if unfit:
        return '待协调', 'part_exceeds_boxes', {'causes': unfit}, None
    plans, blocked = [], set()
    for groups in _candidates(spec):
        plan, error = _build_plan(spec, groups)
        if error:
            blocked.add(error)
        elif not plan['makes_cutoff']:
            blocked.add('cutoff_infeasible')
        else:
            plans.append(plan)
    if plans:
        best = min(plans, key=lambda p: (p['total_price'], len(p['boxes']),
                                         json.dumps(p, sort_keys=True)))
        return '已装箱', 'layout_selected', {}, best
    if blocked:
        reason = 'cutoff_infeasible' if 'cutoff_infeasible' in blocked else 'rate_tier_exceeded'
        return '待协调', reason, {'causes': [{'cause': reason}]}, None
    return _diagnose(spec, available)


def _save_layout(db, key, source, version, spec, best):
    summary = {'kind': 'summary', 'plan': key, 'contract': spec['contract'],
               'total_price': best['total_price'],
               'boxes': [b['box_no'] for b in best['boxes']],
               'ready_at': best['ready_at'], 'cutoff': spec['cutoff'].isoformat(),
               'makes_cutoff': best['makes_cutoff']}
    save(db, 'packing_layout', f'{key}:summary', source, version, summary,
         '已装箱', 'layout_selected')
    for box in best['boxes']:
        body = {'kind': 'box', 'plan': key, 'box_no': box['box_no'],
                'box_type': box['box_type'], 'parts': box['parts'],
                'gross_weight': box['gross_weight'], 'weight_range': box['weight_range'],
                'volumetric_weight': box['volumetric_weight'],
                'chargeable_weight': box['chargeable_weight'],
                'tier_up_to': box['tier_up_to'], 'price': box['price']}
        save(db, 'packing_layout', f"{key}:box:{box['box_no']}", source, version,
             body, '已装箱', 'layout_verified')


def plan(path, file):
    source, payload = capture(path, 'desktop_packing_request', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                try:
                    spec = parse_order(row)
                except Spec as exc:
                    status, reason = '待核', exc.reason
                    detail = {'causes': [exc.detail] if exc.detail else []}
                    best = None
                else:
                    status, reason, detail, best = evaluate(spec)
                save(db, 'packing_plan', key, source, payload['version'], row, status, reason)
                if best:
                    _save_layout(db, key, source, payload['version'], spec, best)
                else:
                    save(db, 'packing_layout', f'{key}:diagnosis', source, payload['version'],
                         {'kind': 'diagnosis', 'plan': key, **detail}, status, reason)
                results.append(decide(db, '制件装箱', key, status, reason, source))
        return results
    finally:
        db.close()
