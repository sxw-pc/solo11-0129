"""离线装箱规划:给出每件制件在箱内可复核的摆放坐标。

采入桌面装箱软件导出的制件外廓、显式允许的三维旋转、缓冲垫厚度、
不可承压易损面、重量,以及箱型内尺寸、承重和真实库存数量,按承运
价阶(实重与体积重较大者)与截单时刻选择总费用最低的可行方案。
全部几何用分数精确计算:拐角与边缘刚好相切不算穿透,缓冲垫跨面
也有确定结果。找不到布局时给出最小不可装组合与约束类别。
"""
import json
import re
from datetime import datetime, timedelta
from fractions import Fraction
from itertools import combinations_with_replacement, permutations, product

from .ingest import capture
from .store import connect, save, decide

FACES = {'+x': (0, 1), '-x': (0, -1), '+y': (1, 1), '-y': (1, -1), '+z': (2, 1), '-z': (2, -1)}
MAX_PARTS = 6
NODE_CAP = 60000
DEFAULT_WEIGHT_TOLERANCE = '0.1'


def F(value):
    """把 JSON 数值解析为精确分数。"""
    return Fraction(str(value))


def _num(frac):
    frac = Fraction(frac)
    return frac.numerator if frac.denominator == 1 else float(frac)


# ---------------------------------------------------------------- 三维旋转

def _perm_sign(perm):
    inversions = sum(1 for i in range(3) for j in range(i + 1, 3) if perm[i] > perm[j])
    return -1 if inversions % 2 else 1


def _build_rotations():
    """全部 24 个正常旋转(行列式 +1 的带符号置换)。

    旋转用 (perm, signs) 表示:world[j] = signs[j] * local[perm[j]]。
    R0 是恒等旋转。镜像(行列式 -1)不在其中,永远非法。
    """
    rotations = []
    for perm in permutations((0, 1, 2)):
        for signs in product((1, -1), repeat=3):
            if _perm_sign(perm) * signs[0] * signs[1] * signs[2] == 1:
                rotations.append((perm, signs))
    return rotations


ROTATIONS = _build_rotations()


def _rotation_matrix(rot):
    perm, signs = rot
    matrix = [[0, 0, 0] for _ in range(3)]
    for j in range(3):
        matrix[j][perm[j]] = signs[j]
    return matrix


def _matrix_to_rotation(matrix):
    perm, signs = [], []
    for row in matrix:
        nonzero = [i for i, v in enumerate(row) if v != 0]
        if len(nonzero) != 1 or abs(row[nonzero[0]]) != 1:
            raise ValueError('invalid_rotation')
        perm.append(nonzero[0])
        signs.append(1 if row[nonzero[0]] > 0 else -1)
    if sorted(perm) != [0, 1, 2]:
        raise ValueError('invalid_rotation')
    det = _perm_sign(tuple(perm)) * signs[0] * signs[1] * signs[2]
    if det == -1:
        raise ValueError('mirror_rotation_not_allowed')
    if det != 1:
        raise ValueError('invalid_rotation')
    return tuple(perm), tuple(signs)


def _rotation_label(rot):
    return f'R{ROTATIONS.index(rot)}'


def _parse_rotation(entry):
    """旋转可以是 'R0'..'R23' 或显式 3x3 带符号置换矩阵(行列式必须 +1)。"""
    if isinstance(entry, str):
        match = re.fullmatch(r'R(\d+)', entry)
        if match and int(match.group(1)) < len(ROTATIONS):
            rot = ROTATIONS[int(match.group(1))]
            return rot, f'R{int(match.group(1))}'
        raise ValueError('unknown_rotation')
    if isinstance(entry, list):
        try:
            matrix = [[int(v) for v in row] for row in entry]
        except (TypeError, ValueError):
            raise ValueError('invalid_rotation')
        if len(matrix) != 3 or any(len(row) != 3 for row in matrix):
            raise ValueError('invalid_rotation')
        rot = _matrix_to_rotation(matrix)
        return rot, _rotation_label(rot)
    raise ValueError('invalid_rotation')


# ---------------------------------------------------------------- 请求校验

def _validate_part(part):
    if not isinstance(part, dict):
        return 'invalid_part', '制件行必须是对象'
    pid = part.get('id', '?')
    try:
        size = [F(v) for v in part['size']]
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return 'invalid_part', f'制件 {pid} 缺少三维外廓 size'
    if len(size) != 3 or any(v <= 0 for v in size):
        return 'invalid_part', f'制件 {pid} 的 size 必须是三个正数(毫米)'
    try:
        weight = F(part['weight'])
        cushion = F(part.get('cushion', 0))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return 'invalid_part', f'制件 {pid} 的 weight/cushion 无法解析'
    if weight <= 0 or cushion < 0:
        return 'invalid_part', f'制件 {pid} 的 weight 必须为正,cushion 不能为负'
    faces = part.get('fragile_faces', [])
    if not isinstance(faces, list) or any(face not in FACES for face in faces):
        return 'invalid_fragile_face', f'制件 {pid} 的 fragile_faces 只能是 +x/-x/+y/-y/+z/-z'
    rotations = part.get('rotations')
    if not isinstance(rotations, list) or not rotations:
        return 'rotations_required', f'制件 {pid} 必须显式列出允许的三维旋转'
    for entry in rotations:
        try:
            _parse_rotation(entry)
        except ValueError as exc:
            return str(exc), f'制件 {pid} 的旋转 {entry!r} 不合法'
    return None


def _validate_box_type(box_type):
    if not isinstance(box_type, dict):
        return 'invalid_box_type', '箱型行必须是对象'
    tid = box_type.get('id', '?')
    try:
        inner = [F(v) for v in box_type['inner']]
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return 'invalid_box_type', f'箱型 {tid} 缺少内尺寸 inner'
    if len(inner) != 3 or any(v <= 0 for v in inner):
        return 'invalid_box_type', f'箱型 {tid} 的 inner 必须是三个正数(毫米)'
    try:
        tare = F(box_type['tare'])
        max_weight = F(box_type['max_weight'])
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return 'invalid_box_type', f'箱型 {tid} 的 tare/max_weight 无法解析'
    if tare < 0 or max_weight <= 0:
        return 'invalid_box_type', f'箱型 {tid} 的 tare 不能为负,max_weight 必须为正'
    qty = box_type.get('quantity')
    if not isinstance(qty, int) or isinstance(qty, bool) or qty < 0:
        return 'invalid_box_quantity', f'箱型 {tid} 的 quantity 必须是非负整数库存'
    try:
        if F(box_type.get('pack_minutes', 0)) < 0:
            raise ValueError
    except (TypeError, ValueError, ArithmeticError):
        return 'invalid_box_type', f'箱型 {tid} 的 pack_minutes 不能为负'
    return None


def _validate_pricing(pricing):
    if not isinstance(pricing, dict):
        return 'invalid_pricing', '缺少 pricing'
    try:
        divisor = F(pricing['volumetric_divisor'])
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return 'invalid_pricing', 'volumetric_divisor 无法解析'
    if divisor <= 0:
        return 'invalid_pricing', 'volumetric_divisor 必须为正'
    tiers = pricing.get('tiers')
    if not isinstance(tiers, list) or not tiers:
        return 'invalid_pricing', 'tiers 不能为空'
    last = Fraction(-1)
    for tier in tiers:
        try:
            up_to = F(tier['up_to'])
            price = F(tier['price'])
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return 'invalid_pricing', '价阶缺少 up_to/price'
        if up_to <= 0 or price < 0 or up_to <= last:
            return 'invalid_pricing', '价阶 up_to 必须递增且为正,price 不能为负'
        last = up_to
    return None


def _validate(payload):
    order = payload.get('order')
    if not isinstance(order, dict) or not order.get('id'):
        return 'invalid_order', '缺少 order.id'
    if order.get('contract') not in ('single', 'multi'):
        return 'invalid_contract', "contract 必须是 'single'(单包裹保管合同) 或 'multi'(分包裹合同)"
    try:
        datetime.fromisoformat(str(order.get('cutoff', '')))
    except ValueError:
        return 'invalid_cutoff', 'order.cutoff 必须是 ISO 时间'
    if order.get('pack_start') is not None:
        try:
            datetime.fromisoformat(str(order['pack_start']))
        except ValueError:
            return 'invalid_pack_start', 'order.pack_start 必须是 ISO 时间'
    parts = payload.get('parts')
    if not isinstance(parts, list) or not parts:
        return 'no_parts', 'parts 不能为空'
    if len(parts) > MAX_PARTS:
        return 'too_many_parts', f'离线核验一次最多 {MAX_PARTS} 件制件'
    for part in parts:
        problem = _validate_part(part)
        if problem:
            return problem
    box_types = payload.get('box_types')
    if not isinstance(box_types, list) or not box_types:
        return 'no_box_types', 'box_types 不能为空'
    for box_type in box_types:
        problem = _validate_box_type(box_type)
        if problem:
            return problem
    problem = _validate_pricing(payload.get('pricing'))
    if problem:
        return problem
    try:
        if F(payload.get('weight_tolerance_kg', DEFAULT_WEIGHT_TOLERANCE)) < 0:
            raise ValueError
    except (TypeError, ValueError, ArithmeticError):
        return 'invalid_weight_tolerance', 'weight_tolerance_kg 必须是非负数'
    return None


# ---------------------------------------------------------------- 模型构建

def _orientations(part):
    """每个允许旋转下的世界系尺寸、含缓冲外盒和易损面朝向。"""
    size, cushion, fragile = part['size'], part['cushion'], part['fragile']
    orients = []
    for rot, label in part['rotations']:
        perm, signs = rot
        dims = tuple(size[perm[j]] for j in range(3))
        eff = tuple(dims[j] + 2 * cushion for j in range(3))
        world_faces = set()
        for axis, sign in fragile:
            j = perm.index(axis)
            world_faces.add((j, sign * signs[j]))
        orients.append({
            'label': label, 'rot': rot, 'dims': dims, 'eff': eff,
            'bottom_ok': (2, -1) not in world_faces,
            'top_ok': (2, 1) not in world_faces,
        })
    return orients


def _build(part_rows, type_rows, pricing_row):
    parts = []
    for row in part_rows:
        part = {
            'id': str(row['id']),
            'size': tuple(F(v) for v in row['size']),
            'weight': F(row['weight']),
            'cushion': F(row.get('cushion', 0)),
            'fragile': {FACES[f] for f in row.get('fragile_faces', [])},
            'rotations': [_parse_rotation(r) for r in row['rotations']],
        }
        part['orients'] = _orientations(part)
        parts.append(part)
    types = [{
        'id': str(t['id']),
        'inner': tuple(F(v) for v in t['inner']),
        'tare': F(t['tare']),
        'max_weight': F(t['max_weight']),
        'quantity': t['quantity'],
        'certified': bool(t.get('certified', False)),
        'pack_minutes': F(t.get('pack_minutes', 0)),
    } for t in type_rows]
    pricing = {
        'volumetric_divisor': F(pricing_row['volumetric_divisor']),
        'tiers': [{'up_to': F(t['up_to']), 'price': F(t['price'])} for t in pricing_row['tiers']],
        'currency': pricing_row.get('currency', 'CNY'),
    }
    return parts, types, pricing


# ---------------------------------------------------------------- 单箱布局

def _covers(rect, tops):
    """rect 是否被同一高度上的顶面矩形集合完全覆盖(精确分数)。"""
    x0, y0, x1, y1 = rect
    xs = {x0, x1}
    for top in tops:
        if x0 < top[0] < x1:
            xs.add(top[0])
        if x0 < top[2] < x1:
            xs.add(top[2])
    xs = sorted(xs)
    for a, b in zip(xs, xs[1:]):
        intervals = []
        for top in tops:
            if top[0] <= a and top[2] >= b:
                lo, hi = max(top[1], y0), min(top[3], y1)
                if lo < hi:
                    intervals.append((lo, hi))
        cover = y0
        for lo, hi in sorted(intervals):
            if lo > cover:
                return False
            cover = max(cover, hi)
        if cover < y1:
            return False
    return True


def _candidates(effs, eff, inner):
    """候选角点:原点与已放外盒各轴最大面的组合,按 (z, y, x) 排序。"""
    xs = {Fraction(0)} | {b[3] for b in effs}
    ys = {Fraction(0)} | {b[4] for b in effs}
    zs = {Fraction(0)} | {b[5] for b in effs}
    out = []
    for z in sorted(zs):
        for y in sorted(ys):
            for x in sorted(xs):
                if x + eff[0] <= inner[0] and y + eff[1] <= inner[1] and z + eff[2] <= inner[2]:
                    out.append((x, y, z))
    return out


def _overlaps(effs, nb):
    """严格穿透才算冲突;面、棱、角刚好相切不算。"""
    return any(all(nb[i] < b[i + 3] and b[i] < nb[i + 3] for i in range(3)) for b in effs)


def _supported(effs, pos, eff):
    """底面必须由箱底或同高度其他外盒顶面完全承托(不允许悬空)。"""
    if pos[2] == 0:
        return True
    tops = [(b[0], b[1], b[3], b[4]) for b in effs if b[5] == pos[2]]
    return _covers((pos[0], pos[1], pos[0] + eff[0], pos[1] + eff[1]), tops)


def _crushes(effs, top_ok, pos, eff):
    """新外盒底面是否压在某个标注不可承压的顶面上(按含缓冲外盒计)。"""
    for b, ok in zip(effs, top_ok):
        if ok or b[5] != pos[2]:
            continue
        if (min(b[3], pos[0] + eff[0]) > max(b[0], pos[0])
                and min(b[4], pos[1] + eff[1]) > max(b[1], pos[1])):
            return True
    return False


def _pack_box(members, inner, check_fragile=True, node_cap=NODE_CAP):
    """确定性回溯装箱。返回 (layout, stats);不可装时 layout 为 None。

    制件放置顺序、姿态顺序(按导出列出的顺序)与角点顺序均固定,
    第一个可行解即结果,可复现、可复核。
    """
    ordered = sorted(members, key=lambda p: (
        -max(o['eff'][0] * o['eff'][1] * o['eff'][2] for o in p['orients']),
        -p['weight'], p['id']))
    placed, effs, top_ok = [], [], []
    stats = {'overlap': 0, 'support': 0, 'fragile_crush': 0, 'fragile_bottom': 0, 'nodes': 0}
    exceeded = []

    def backtrack(remaining):
        if not remaining:
            return True
        stats['nodes'] += 1
        if stats['nodes'] > node_cap:
            exceeded.append(True)
            return False
        for part in remaining:
            for orient in part['orients']:
                if check_fragile and not orient['bottom_ok']:
                    stats['fragile_bottom'] += 1
                    continue
                eff = orient['eff']
                for pos in _candidates(effs, eff, inner):
                    nb = (pos[0], pos[1], pos[2],
                          pos[0] + eff[0], pos[1] + eff[1], pos[2] + eff[2])
                    if _overlaps(effs, nb):
                        stats['overlap'] += 1
                        continue
                    if not _supported(effs, pos, eff):
                        stats['support'] += 1
                        continue
                    if check_fragile and _crushes(effs, top_ok, pos, eff):
                        stats['fragile_crush'] += 1
                        continue
                    placed.append({'part': part, 'orient': orient, 'pos': pos})
                    effs.append(nb)
                    top_ok.append(orient['top_ok'])
                    if backtrack(tuple(p for p in remaining if p is not part)):
                        return True
                    placed.pop()
                    effs.pop()
                    top_ok.pop()
                    if exceeded:
                        return False
        return False

    if backtrack(tuple(ordered)):
        return placed, stats
    return None, stats


# ---------------------------------------------------------------- 计费与求解

def _volumetric(inner, divisor):
    """体积重(kg)= 箱型内尺寸体积(cm^3) / 除数,作为线下计费近似。"""
    return inner[0] * inner[1] * inner[2] / (1000 * divisor)


def _tier(tiers, chargeable):
    for tier in tiers:
        if chargeable <= tier['up_to']:
            return tier
    return None


def _cand_key(cand):
    return (cand['cost'], cand['n'], cand['combo'], cand['key'])


def _solve(parts, types, contract, pricing, pack_start, cutoff,
           ignore_weight=False, ignore_fragile=False, ignore_tier=False):
    """枚举箱数、箱型组合与制件分配,返回费用最低且赶上截单的候选。

    单包裹保管合同:恰好一只认证箱;分包裹合同:最多两只合格箱。
    """
    max_boxes = 1 if contract == 'single' else 2
    eligible = [t for t in types if t['quantity'] > 0 and (contract == 'multi' or t['certified'])]
    by_id = {t['id']: t for t in types}
    best = None
    for n in range(1, max_boxes + 1):
        ids = sorted(t['id'] for t in eligible)
        combos = [c for c in combinations_with_replacement(ids, n)
                  if all(c.count(t['id']) <= t['quantity'] for t in eligible)]
        for combo in combos:
            seen = set()
            for assign in product(range(n), repeat=len(parts)):
                if n > 1 and len(set(assign)) < n:
                    continue
                groups = [tuple(sorted(parts[i]['id'] for i in range(len(parts)) if assign[i] == b))
                          for b in range(n)]
                key = tuple(sorted(zip(combo, groups)))
                if key in seen:
                    continue
                seen.add(key)
                boxes = []
                feasible = True
                for b in range(n):
                    box_type = by_id[combo[b]]
                    members = [parts[i] for i in range(len(parts)) if assign[i] == b]
                    gross = box_type['tare'] + sum((p['weight'] for p in members), Fraction(0))
                    if not ignore_weight and gross > box_type['max_weight']:
                        feasible = False
                        break
                    layout, _stats = _pack_box(members, box_type['inner'],
                                               check_fragile=not ignore_fragile)
                    if layout is None:
                        feasible = False
                        break
                    vol = _volumetric(box_type['inner'], pricing['volumetric_divisor'])
                    chargeable = max(gross, vol)
                    tier = _tier(pricing['tiers'], chargeable)
                    if tier is None and not ignore_tier:
                        feasible = False
                        break
                    boxes.append({'box_type': box_type['id'], 'members': members, 'layout': layout,
                                  'inner': box_type['inner'], 'gross': gross, 'vol': vol,
                                  'chargeable': chargeable,
                                  'tier_up_to': tier['up_to'] if tier else None,
                                  'price': tier['price'] if tier else Fraction(0),
                                  'pack_minutes': box_type['pack_minutes']})
                if not feasible:
                    continue
                cost = sum((bx['price'] for bx in boxes), Fraction(0))
                ready = None
                if pack_start is not None:
                    minutes = float(sum((bx['pack_minutes'] for bx in boxes), Fraction(0)))
                    ready = pack_start + timedelta(minutes=minutes)
                    if cutoff is not None and ready > cutoff:
                        continue
                cand = {'cost': cost, 'n': n, 'combo': combo, 'key': key,
                        'ready': ready, 'boxes': boxes}
                if best is None or _cand_key(cand) < _cand_key(best):
                    best = cand
    return best


# ---------------------------------------------------------------- 不可装诊断

def _minimal_subset(parts, feasible):
    """贪婪删除得到一极小不可装子集(按制件 id 确定顺序)。"""
    subset = list(parts)
    for part in sorted(parts, key=lambda p: p['id']):
        if len(subset) > 1:
            trial = [q for q in subset if q is not part]
            if not feasible(trial):
                subset = trial
    return subset


def _diagnose(parts, types, contract, pricing, pack_start, cutoff):
    """指出哪件制件、哪项方向/承重/库存约束形成最小不可装组合。"""
    eligible = [t for t in types if t['quantity'] > 0 and (contract == 'multi' or t['certified'])]
    if not eligible:
        code = 'no_certified_box' if contract == 'single' else 'no_box_available'
        detail = ('单包裹保管合同要求经认证的箱型,库存中没有可用认证箱'
                  if contract == 'single' else '库存中没有可用箱型')
        return {'code': code, 'parts': [p['id'] for p in parts],
                'constraint': 'box_inventory', 'detail': detail}
    for part in sorted(parts, key=lambda p: p['id']):
        fits = any(all(o['eff'][j] <= t['inner'][j] for j in range(3))
                   for t in eligible for o in part['orients'])
        if not fits:
            return {'code': 'part_exceeds_all_boxes', 'parts': [part['id']],
                    'constraint': 'dimensions',
                    'detail': f"制件 {part['id']} 加缓冲后的任何允许姿态都越出所有合格箱型的内尺寸"}

    def feasible(members, **flags):
        return _solve(members, types, contract, pricing, pack_start, cutoff, **flags) is not None

    def minimal():
        return [p['id'] for p in _minimal_subset(parts, feasible)]

    unlimited = [dict(t, quantity=len(parts)) for t in types]
    if _solve(parts, unlimited, contract, pricing, pack_start, cutoff) is not None:
        return {'code': 'box_inventory_insufficient', 'parts': minimal(),
                'constraint': 'box_inventory',
                'detail': '箱型真实库存不足,无法按合同允许的箱数分装'}
    if feasible(parts, ignore_weight=True):
        ids = minimal()
        return {'code': 'combined_weight_exceeded', 'parts': ids, 'constraint': 'weight',
                'detail': f"最小不可装组合 {'、'.join(ids)}:合计重量超出任何合格箱型的承重"}
    if feasible(parts, ignore_fragile=True):
        ids = minimal()
        return {'code': 'fragile_support_conflict', 'parts': ids, 'constraint': 'fragile_support',
                'detail': f"最小不可装组合 {'、'.join(ids)}:同箱只能叠放,且会压在标注不可承压的易损面上"}
    if feasible(parts, ignore_tier=True):
        ids = minimal()
        return {'code': 'chargeable_weight_exceeds_tiers', 'parts': ids, 'constraint': 'price_tier',
                'detail': f"最小不可装组合 {'、'.join(ids)}:计费重量超出所有价阶"}
    ids = minimal()
    return {'code': 'no_feasible_layout', 'parts': ids, 'constraint': 'dimensions',
            'detail': f"最小不可装组合 {'、'.join(ids)}:加缓冲后的外廓在允许姿态下无法无穿透地放入可用箱型"}


# ---------------------------------------------------------------- 库存序列号

def _consumed_serials(db, order_id):
    """已被占用的实体箱号:其他订单占用过的,以及本订单已交运的。"""
    consumed = {}
    shipped = {r['id'] for r in db.execute(
        "SELECT id FROM carrier_evidence WHERE status='已交运'")}
    for row in db.execute('SELECT id, body FROM packing_box'):
        body = json.loads(row['body'])
        if body.get('order') != order_id or row['id'] in shipped:
            consumed.setdefault(body.get('box_type'), set()).add(row['id'])
    return consumed


# ---------------------------------------------------------------- 入口

def plan(path, file):
    source, payload = capture(path, 'desktop_packing_request', file)
    db = connect(path)
    try:
        with db:
            return [_plan_one(db, source, payload)]
    finally:
        db.close()


def _plan_one(db, source, payload):
    version = payload['version']
    order = payload.get('order') if isinstance(payload.get('order'), dict) else {}
    order_id = str(order.get('id', 'unknown'))
    plan_id = f'{order_id}@{version}'
    problem = _validate(payload)
    if problem:
        reason, detail = problem
        diagnosis = {'code': reason, 'parts': [], 'constraint': 'request', 'detail': detail}
        body = {'plan_id': plan_id, 'order': order_id, 'contract': order.get('contract'),
                'diagnosis': diagnosis, 'boxes': []}
        save(db, 'packing_plan', plan_id, source, version, body, '待核', reason)
        result = decide(db, '装箱方案', order_id, '待核', reason, source)
        result.update(plan_id=plan_id, diagnosis=diagnosis)
        return result
    contract = order['contract']
    cutoff = datetime.fromisoformat(str(order['cutoff']))
    pack_start = (datetime.fromisoformat(str(order['pack_start']))
                  if order.get('pack_start') else None)
    parts, types, pricing = _build(payload['parts'], payload['box_types'], payload['pricing'])
    consumed = _consumed_serials(db, order_id)
    available = [dict(t, quantity=t['quantity'] - len(consumed.get(t['id'], ())))
                 for t in types]
    best = _solve(parts, available, contract, pricing, pack_start, cutoff)
    if best is None:
        diagnosis = _diagnose(parts, available, contract, pricing, pack_start, cutoff)
        body = {'plan_id': plan_id, 'order': order_id, 'contract': contract,
                'cutoff': order['cutoff'], 'diagnosis': diagnosis, 'boxes': []}
        save(db, 'packing_plan', plan_id, source, version, body, '待协调', diagnosis['code'])
        result = decide(db, '装箱方案', order_id, '待协调', diagnosis['code'], source)
        result.update(plan_id=plan_id, diagnosis=diagnosis)
        return result
    counters = {}

    def next_serial(box_type):
        k = counters.get(box_type, 1)
        while f'{box_type}#{k}' in consumed.get(box_type, ()):
            k += 1
        counters[box_type] = k + 1
        return f'{box_type}#{k}'

    boxes_out = []
    ordered_boxes = sorted(best['boxes'],
                           key=lambda b: (b['box_type'], [m['id'] for m in b['members']]))
    for bx in ordered_boxes:
        serial = next_serial(bx['box_type'])
        layout = []
        for placed in bx['layout']:
            part, orient, pos = placed['part'], placed['orient'], placed['pos']
            cushion = part['cushion']
            layout.append({
                'part': part['id'],
                'rotation': orient['label'],
                'rotation_matrix': _rotation_matrix(orient['rot']),
                'position': [_num(pos[j] + cushion) for j in range(3)],
                'dims': [_num(orient['dims'][j]) for j in range(3)],
                'cushion': _num(cushion),
                'eff_min': [_num(pos[j]) for j in range(3)],
                'eff_max': [_num(pos[j] + orient['eff'][j]) for j in range(3)],
            })
        layout.sort(key=lambda item: item['part'])
        boxes_out.append({
            'serial': serial, 'box_type': bx['box_type'], 'parts': layout,
            'inner': [_num(v) for v in bx['inner']],
            'gross_kg': _num(bx['gross']), 'volumetric_kg': _num(bx['vol']),
            'chargeable_kg': _num(bx['chargeable']),
            'tier_up_to': _num(bx['tier_up_to']), 'price': _num(bx['price']),
        })
    body = {
        'plan_id': plan_id, 'order': order_id, 'contract': contract,
        'cutoff': order['cutoff'], 'pack_start': order.get('pack_start'),
        'ready_by': best['ready'].isoformat() if best['ready'] else None,
        'weight_tolerance_kg': _num(F(payload.get('weight_tolerance_kg', DEFAULT_WEIGHT_TOLERANCE))),
        'currency': pricing['currency'], 'total_price': _num(best['cost']),
        'boxes': boxes_out, 'diagnosis': None,
    }
    save(db, 'packing_plan', plan_id, source, version, body, '已装箱', 'layout_verified')
    for bx in boxes_out:
        allocation = {'plan_id': plan_id, 'order': order_id, 'box_type': bx['box_type'],
                      'serial': bx['serial'], 'parts': [p['part'] for p in bx['parts']],
                      'gross_kg': bx['gross_kg'], 'chargeable_kg': bx['chargeable_kg'],
                      'tier_up_to': bx['tier_up_to'], 'price': bx['price'],
                      'layout': bx['parts']}
        db.execute('INSERT OR REPLACE INTO packing_box(id,source_id,version,body,status,reason)'
                   ' VALUES (?,?,?,?,?,?)',
                   (bx['serial'], source, version,
                    json.dumps(allocation, sort_keys=True, ensure_ascii=False),
                    '已装箱', 'allocated'))
    result = decide(db, '装箱方案', order_id, '已装箱', 'layout_verified', source)
    result.update(plan_id=plan_id, total_price=body['total_price'], currency=body['currency'],
                  ready_by=body['ready_by'],
                  boxes=[{'serial': b['serial'], 'box_type': b['box_type'],
                          'parts': [p['part'] for p in b['parts']],
                          'gross_kg': b['gross_kg'], 'chargeable_kg': b['chargeable_kg'],
                          'tier_up_to': b['tier_up_to'], 'price': b['price']}
                         for b in boxes_out])
    return result
