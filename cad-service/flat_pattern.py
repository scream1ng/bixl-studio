"""
Pure-Python flat pattern unfolder for sheet-metal STEP files.
No OCC/pythonocc required. Uses regex-based STEP entity parsing.

Algorithm:
  1. Parse STEP topology: faces, edges, curves, vertices
  2. Build face adjacency via shared edge curves
  3. BFS from largest planar face; unfold each panel across each bend
  4. Project all faces into a common 2D plane
  5. Render SVG with bend lines and dimensions
"""

import re
import math
import io
from collections import defaultdict, deque
from typing import Dict, List, Optional, Set, Tuple

Vec3 = Tuple[float, float, float]
Vec2 = Tuple[float, float]

# ─── Vec3 math ─────────────────────────────────────────────────────────────────

def _add(a: Vec3, b: Vec3) -> Vec3: return (a[0]+b[0], a[1]+b[1], a[2]+b[2])
def _sub(a: Vec3, b: Vec3) -> Vec3: return (a[0]-b[0], a[1]-b[1], a[2]-b[2])
def _scale(v: Vec3, s: float) -> Vec3: return (v[0]*s, v[1]*s, v[2]*s)
def _dot(a: Vec3, b: Vec3) -> float: return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]
def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
def _norm(v: Vec3) -> Vec3:
    m = math.sqrt(_dot(v, v))
    return _scale(v, 1/m) if m > 1e-12 else (0.0, 0.0, 0.0)
def _len(v: Vec3) -> float: return math.sqrt(_dot(v, v))

def _project2d(pt: Vec3, origin: Vec3, u: Vec3, v: Vec3) -> Vec2:
    d = _sub(pt, origin)
    return (_dot(d, u), _dot(d, v))

def _rotate_point(pt: Vec3, axis: Vec3, angle: float, pivot: Vec3) -> Vec3:
    """Rotate pt around axis (unit vec) through pivot by angle (radians)."""
    d = _sub(pt, pivot)
    ax, ay, az = axis
    c, s = math.cos(angle), math.sin(angle)
    t = 1 - c
    rx = (t*ax*ax+c)*d[0] + (t*ax*ay-s*az)*d[1] + (t*ax*az+s*ay)*d[2]
    ry = (t*ax*ay+s*az)*d[0] + (t*ay*ay+c)*d[1] + (t*ay*az-s*ax)*d[2]
    rz = (t*ax*az-s*ay)*d[0] + (t*ay*az+s*ax)*d[1] + (t*az*az+c)*d[2]
    return _add(pivot, (rx, ry, rz))

# ─── STEP parser ───────────────────────────────────────────────────────────────

def _flatten(text: str) -> str:
    return re.sub(r'\r?\n\s*', ' ', text)

def _split_top(args: str) -> List[str]:
    """Split at top-level commas only (respects nested parens)."""
    parts, depth, buf = [], 0, []
    for ch in args:
        if ch == '(':
            depth += 1; buf.append(ch)
        elif ch == ')':
            depth -= 1; buf.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(buf).strip()); buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append(''.join(buf).strip())
    return parts

def _ids(s: str) -> List[str]:
    return re.findall(r'#(\d+)', s)

def _floats(s: str) -> List[float]:
    cleaned = re.sub(r'#\d+', '', s)  # remove entity refs before extracting numbers
    return [float(x) for x in re.findall(r'[+-]?(?:\d+\.?\d*|\.\d+)(?:[Ee][+-]?\d+)?', cleaned)]

def _parse_entities(text: str) -> Dict[str, Tuple[str, str]]:
    flat = _flatten(text)
    result = {}
    for m in re.finditer(r'#(\d+)\s*=\s*([A-Z_0-9]+)\s*\(([^;]*)\)\s*;', flat):
        result[m.group(1)] = (m.group(2), m.group(3))
    return result

# ─── StepModel ─────────────────────────────────────────────────────────────────

class StepModel:
    def __init__(self, path: str):
        text = open(path, 'r', errors='replace').read()
        ents = _parse_entities(text)
        self._ents = ents
        s = self._detect_scale(ents)

        # Cartesian points
        self.pts: Dict[str, Vec3] = {}
        for eid, (et, args) in ents.items():
            if et == 'CARTESIAN_POINT':
                fs = _floats(args)
                if len(fs) >= 3:
                    self.pts[eid] = (fs[0]*s, fs[1]*s, fs[2]*s)

        # Directions
        self.dirs: Dict[str, Vec3] = {}
        for eid, (et, args) in ents.items():
            if et == 'DIRECTION':
                fs = _floats(args)
                if len(fs) >= 3:
                    self.dirs[eid] = (fs[0], fs[1], fs[2])

        # AXIS2_PLACEMENT_3D
        self.ax2: Dict[str, dict] = {}
        for eid, (et, args) in ents.items():
            if et == 'AXIS2_PLACEMENT_3D':
                ids = _ids(args)
                if len(ids) >= 3:
                    self.ax2[eid] = {
                        'loc': self.pts.get(ids[0], (0,0,0)),
                        'axis': self.dirs.get(ids[1], (0,0,1)),
                        'ref': self.dirs.get(ids[2], (1,0,0)),
                    }

        # Surfaces
        self.planes: Dict[str, dict] = {}
        self.cylinders: Dict[str, dict] = {}
        for eid, (et, args) in ents.items():
            if et == 'PLANE':
                aid = _ids(args)
                if aid and aid[0] in self.ax2:
                    ap = self.ax2[aid[0]]
                    self.planes[eid] = {'origin': ap['loc'], 'normal': ap['axis'], 'ref': ap['ref']}
            elif et == 'CYLINDRICAL_SURFACE':
                ids = _ids(args); fs = _floats(args)
                if ids and fs and ids[0] in self.ax2:
                    ap = self.ax2[ids[0]]
                    self.cylinders[eid] = {
                        'origin': ap['loc'], 'axis': ap['axis'], 'ref': ap['ref'],
                        'radius': fs[0] * s,
                    }

        # Curves
        self.lines: Dict[str, dict] = {}
        self.circles: Dict[str, dict] = {}
        for eid, (et, args) in ents.items():
            if et == 'LINE':
                ids = _ids(args)
                if len(ids) >= 2:
                    p = self.pts.get(ids[0]); d = self.dirs.get(ids[1])
                    if p and d:
                        self.lines[eid] = {'pt': p, 'dir': _norm(d)}
            elif et == 'CIRCLE':
                ids = _ids(args); fs = _floats(args)
                if ids and fs and ids[0] in self.ax2:
                    ap = self.ax2[ids[0]]
                    self.circles[eid] = {
                        'center': ap['loc'], 'normal': ap['axis'], 'ref': ap['ref'],
                        'radius': fs[0] * s,
                    }

        # Vertices
        self.verts: Dict[str, Vec3] = {}
        for eid, (et, args) in ents.items():
            if et == 'VERTEX_POINT':
                ids = _ids(args)
                if ids and ids[0] in self.pts:
                    self.verts[eid] = self.pts[ids[0]]

        # Edge curves
        self.edge_curves: Dict[str, dict] = {}
        for eid, (et, args) in ents.items():
            if et == 'EDGE_CURVE':
                ids = _ids(args)
                if len(ids) >= 3:
                    self.edge_curves[eid] = {'v0': ids[0], 'v1': ids[1], 'curve': ids[2]}

        # Oriented edges
        self.oriented_edges: Dict[str, dict] = {}
        for eid, (et, args) in ents.items():
            if et == 'ORIENTED_EDGE':
                ids = _ids(args)
                sense = args.strip().endswith('.T.')
                if ids:
                    self.oriented_edges[eid] = {'ec': ids[-1], 'sense': sense}

        # Edge loops
        self.edge_loops: Dict[str, List[str]] = {}
        for eid, (et, args) in ents.items():
            if et == 'EDGE_LOOP':
                self.edge_loops[eid] = _ids(args)

        # Face bounds
        self.face_bounds: Dict[str, str] = {}  # fb_id → loop_id
        for eid, (et, args) in ents.items():
            if et in ('FACE_BOUND', 'FACE_OUTER_BOUND'):
                ids = _ids(args)
                if ids:
                    self.face_bounds[eid] = ids[0]

        # Advanced faces
        self.faces: Dict[str, dict] = {}
        for eid, (et, args) in ents.items():
            if et == 'ADVANCED_FACE':
                parts = _split_top(args)
                if len(parts) >= 3:
                    bound_ids = _ids(parts[1])
                    surf_ids = _ids(parts[2])
                    sense = '.T.' in parts[-1] if parts else True
                    if surf_ids:
                        self.faces[eid] = {
                            'bounds': bound_ids,
                            'surface': surf_ids[0],
                            'sense': sense,
                        }

    @staticmethod
    def _detect_scale(ents: dict) -> float:
        for eid, (et, args) in ents.items():
            if et == 'CARTESIAN_POINT':
                for v in _floats(args):
                    if 0 < abs(v) < 0.5:
                        return 1000.0
                    if abs(v) > 1.0:
                        return 1.0
        return 1.0

    def face_kind(self, fid: str) -> str:
        if fid not in self.faces:
            return 'other'
        s = self.faces[fid]['surface']
        if s in self.planes:
            return 'plane'
        if s in self.cylinders:
            return 'cylinder'
        return 'other'

    def face_normal_outward(self, fid: str) -> Optional[Vec3]:
        if fid not in self.faces:
            return None
        face = self.faces[fid]
        surf = face['surface']
        if surf not in self.planes:
            return None
        n = self.planes[surf]['normal']
        return n if face['sense'] else (-n[0], -n[1], -n[2])

    def face_plane_data(self, fid: str) -> Optional[dict]:
        if fid not in self.faces:
            return None
        surf = self.faces[fid]['surface']
        return self.planes.get(surf)

    def face_cylinder_data(self, fid: str) -> Optional[dict]:
        if fid not in self.faces:
            return None
        surf = self.faces[fid]['surface']
        return self.cylinders.get(surf)

    def face_edge_curves(self, fid: str) -> List[str]:
        """All edge_curve ids for a face (deduplicated)."""
        result = []
        seen: Set[str] = set()
        for fb_id in self.faces.get(fid, {}).get('bounds', []):
            loop_id = self.face_bounds.get(fb_id)
            if not loop_id:
                continue
            for oe_id in self.edge_loops.get(loop_id, []):
                ec = self.oriented_edges.get(oe_id, {}).get('ec')
                if ec and ec not in seen:
                    seen.add(ec); result.append(ec)
        return result

    def build_adjacency(self) -> Dict[str, List[Tuple[str, str]]]:
        """Return {face_id: [(adjacent_face_id, shared_ec_id), ...]}."""
        ec_to_faces: Dict[str, List[str]] = defaultdict(list)
        for fid in self.faces:
            for ec in self.face_edge_curves(fid):
                ec_to_faces[ec].append(fid)
        adj: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        for ec, faces in ec_to_faces.items():
            for i, f1 in enumerate(faces):
                for f2 in faces[i+1:]:
                    if f1 != f2:
                        adj[f1].append((f2, ec))
                        adj[f2].append((f1, ec))
        return dict(adj)

    def discretize_edge(self, ec_id: str, n: int = 24) -> List[Vec3]:
        """Return 3D points along an edge curve."""
        if ec_id not in self.edge_curves:
            return []
        ec = self.edge_curves[ec_id]
        p0 = self.verts.get(ec['v0'])
        p1 = self.verts.get(ec['v1'])
        if not p0 or not p1:
            return []
        cid = ec['curve']
        if cid in self.lines:
            return [p0, p1]
        if cid in self.circles:
            circ = self.circles[cid]
            c = circ['center']
            u = _norm(circ['ref'])
            v = _norm(_cross(circ['normal'], u))
            r = circ['radius']
            def angle(pt): d=_sub(pt,c); return math.atan2(_dot(d,v), _dot(d,u))
            a0, a1 = angle(p0), angle(p1)
            if a1 < a0: a1 += 2*math.pi
            pts = []
            for i in range(n+1):
                t = i / n
                a = a0 + t*(a1-a0)
                pts.append(_add(c, _add(_scale(u, r*math.cos(a)), _scale(v, r*math.sin(a)))))
            return pts
        return [p0, p1]

    def face_3d_outline(self, fid: str) -> List[List[Vec3]]:
        """Return list of polylines (one per bound) for a face."""
        if fid not in self.faces:
            return []
        result = []
        for fb_id in self.faces[fid]['bounds']:
            loop_id = self.face_bounds.get(fb_id)
            if not loop_id:
                continue
            loop_pts: List[Vec3] = []
            for oe_id in self.edge_loops.get(loop_id, []):
                ec_id = self.oriented_edges.get(oe_id, {}).get('ec')
                sense = self.oriented_edges.get(oe_id, {}).get('sense', True)
                if not ec_id:
                    continue
                pts = self.discretize_edge(ec_id)
                if not sense:
                    pts = pts[::-1]
                if loop_pts and pts:
                    loop_pts.extend(pts[1:])  # skip first point (= last of previous edge)
                else:
                    loop_pts.extend(pts)
            if loop_pts:
                result.append(loop_pts)
        return result

    def face_area_approx(self, fid: str) -> float:
        """Approximate face area from bounding box of its points."""
        all_pts = [pt for poly in self.face_3d_outline(fid) for pt in poly]
        if len(all_pts) < 3:
            return 0.0
        xs = [p[0] for p in all_pts]; ys = [p[1] for p in all_pts]; zs = [p[2] for p in all_pts]
        dx = max(xs)-min(xs); dy = max(ys)-min(ys); dz = max(zs)-min(zs)
        dims = sorted([dx, dy, dz], reverse=True)
        return dims[0] * dims[1]  # approx as rect

# ─── Unfolding ─────────────────────────────────────────────────────────────────

def _detect_thickness(model: StepModel) -> float:
    """Estimate sheet thickness from paired cylinder radii or plane spacings."""
    # Try cylinder pairs (inner+outer of same bend)
    cyl_list = list(model.cylinders.values())
    votes: Dict[float, int] = {}
    for i, c1 in enumerate(cyl_list):
        for c2 in cyl_list[i+1:]:
            # Same axis direction?
            if abs(abs(_dot(c1['axis'], c2['axis'])) - 1.0) > 0.05:
                continue
            # Same axis location?
            d = _sub(c2['origin'], c1['origin'])
            perp = _sub(d, _scale(c1['axis'], _dot(d, c1['axis'])))
            if _len(perp) > 2.0:
                continue
            diff = round(abs(c2['radius'] - c1['radius']), 1)
            if 0.3 < diff < 20.0:
                votes[diff] = votes.get(diff, 0) + 1

    if votes:
        best = max(votes, key=votes.get)
        if votes[best] >= 1:
            return best

    # Fallback: plane spacing votes
    planes = list(model.planes.values())
    for i, p1 in enumerate(planes):
        for p2 in planes[i+1:]:
            dot = _dot(p1['normal'], p2['normal'])
            if abs(abs(dot) - 1.0) < 0.01:
                d2 = p2['origin'] if dot > 0 else (-p2['origin'][0], -p2['origin'][1], -p2['origin'][2])
                dist = round(abs(_dot(p1['normal'], _sub(p1['origin'], p2['origin']))), 1)
                if 0.3 < dist < 15.0:
                    votes[dist] = votes.get(dist, 0) + 1
    if votes:
        return min(d for d, v in votes.items() if v == max(votes.values()))
    return 1.6  # fallback: 1.6mm


def _bend_angle_from_normals(n1: Vec3, n2: Vec3) -> float:
    """Bend angle in radians from the two arm normals."""
    dot = max(-1.0, min(1.0, _dot(n1, n2)))
    return math.acos(dot)


def _hinge_line_from_shared_edge(model: StepModel, ec_id: str) -> Optional[Tuple[Vec3, Vec3]]:
    """Return (point, direction) of hinge line from a shared edge curve."""
    if ec_id not in model.edge_curves:
        return None
    ec = model.edge_curves[ec_id]
    p0 = model.verts.get(ec['v0'])
    p1 = model.verts.get(ec['v1'])
    if not p0 or not p1:
        return None
    d = _sub(p1, p0)
    if _len(d) < 1e-9:
        return None
    return (p0, _norm(d))


class _FaceNode:
    """A face with its accumulated 2D transform for flat pattern."""
    def __init__(self, fid: str, transform_3d=None):
        self.fid = fid
        # transform_3d: function Vec3→Vec3 to put this face flat before projection
        self.transform_3d = transform_3d or (lambda p: p)


def unfold(
    path: str,
    k_factor: float = 0.33,
    ignore_small_faces: bool = True,
) -> dict:
    """
    Unfold a sheet-metal STEP file to flat pattern.

    Returns:
        {
          'thickness_mm': float,
          'bends': [{'angle_deg': float, 'radius_mm': float, 'ba_mm': float}, ...],
          'flat_outlines': [[(x, y), ...], ...],  # 2D outlines after unfolding
          'bend_lines': [[(x1,y1),(x2,y2)], ...],  # dashed bend lines in 2D
          'bbox_mm': (w, h),
          'svg': str,
        }
    """
    model = StepModel(path)
    thickness = _detect_thickness(model)

    adj = model.build_adjacency()

    # Classify faces
    plane_faces = {fid for fid in model.faces if model.face_kind(fid) == 'plane'}
    cyl_faces = {fid for fid in model.faces if model.face_kind(fid) == 'cylinder'}

    # Filter out tiny faces (holes annuli etc.) by bounding box area
    def approx_area(fid):
        pts = [pt for poly in model.face_3d_outline(fid) for pt in poly]
        if len(pts) < 3:
            return 0.0
        xs=[p[0] for p in pts]; ys=[p[1] for p in pts]; zs=[p[2] for p in pts]
        dxyz = sorted([max(xs)-min(xs), max(ys)-min(ys), max(zs)-min(zs)], reverse=True)
        return dxyz[0] * dxyz[1]

    areas = {fid: approx_area(fid) for fid in plane_faces}
    if areas:
        max_area = max(areas.values())
        if ignore_small_faces:
            min_area = max_area * 0.02  # ignore faces < 2% of largest
            plane_faces = {fid for fid in plane_faces if areas.get(fid, 0) >= min_area}

    # Pick base face = largest planar face
    if not plane_faces:
        raise ValueError("No planar faces found in STEP file")
    base_fid = max(plane_faces, key=lambda fid: areas.get(fid, 0))

    # BFS: unfold the face graph
    # Each "step" in BFS crosses a cylindrical face to reach the next planar face
    # We track the accumulated 3D→3D transform for each visited planar face

    # For the base face: identity transform, establish 2D coordinate system
    base_plane = model.face_plane_data(base_fid)
    if not base_plane:
        raise ValueError("Base face has no plane data")

    base_normal = model.face_normal_outward(base_fid) or base_plane['normal']
    base_origin = base_plane['origin']
    base_ref = _norm(base_plane['ref'])
    base_vdir = _norm(_cross(base_normal, base_ref))

    # transforms[fid] = function that maps 3D point of that face to a 3D point in base-plane coordinates
    transforms: Dict[str, any] = {base_fid: lambda p: p}
    normals_3d: Dict[str, Vec3] = {base_fid: base_normal}  # outward normal after transform (for computing next bend)
    visited: Set[str] = {base_fid}
    queue: deque = deque([base_fid])

    bend_specs = []  # accumulate bend specs for reporting

    while queue:
        cur_fid = queue.popleft()
        cur_transform = transforms[cur_fid]
        cur_normal = normals_3d[cur_fid]

        for (neighbor_fid, shared_ec) in adj.get(cur_fid, []):
            if neighbor_fid in visited:
                continue

            # Determine if the neighbor is a cylindrical face (bend) or planar face
            kind = model.face_kind(neighbor_fid)

            if kind == 'cylinder':
                # Found a bend face. Now find its other planar neighbor.
                cyl_data = model.face_cylinder_data(neighbor_fid)
                if not cyl_data:
                    continue
                # Slot-end cap check: very short cylinder relative to its radius.
                # Compute height = range of vertex positions along cylinder axis.
                _cyl_axis_dir = _norm(cyl_data['axis'])
                _cyl_origin = cyl_data['origin']
                _projs = []
                for _ec_id in model.face_edge_curves(neighbor_fid):
                    _ec = model.edge_curves.get(_ec_id, {})
                    for _vid in (_ec.get('v0',''), _ec.get('v1','')):
                        _vpt = model.verts.get(_vid)
                        if _vpt:
                            _projs.append(_dot(_sub(_vpt, _cyl_origin), _cyl_axis_dir))
                _cyl_height = (max(_projs) - min(_projs)) if len(_projs) >= 2 else 0.0
                # Skip if height << radius: this is a slot-end cap, not a fold
                if _cyl_height < max(cyl_data['radius'] * 0.3, 2.0 * thickness):
                    continue

                # Find the planar face on the other side of this bend
                next_planar = None
                next_shared_ec = None
                for (nn_fid, nn_ec) in adj.get(neighbor_fid, []):
                    if nn_fid == cur_fid or nn_fid in visited:
                        continue
                    if model.face_kind(nn_fid) == 'plane' and nn_fid in plane_faces:
                        next_planar = nn_fid
                        next_shared_ec = nn_ec
                        break

                if not next_planar:
                    visited.add(neighbor_fid)
                    continue

                # Get hinge line: shared edge between next_planar and cyl
                # (the B-side tangent — this is the pivot for unfolding face B)
                hinge = _hinge_line_from_shared_edge(model, next_shared_ec)
                if not hinge:
                    # fall back to A-side hinge
                    hinge = _hinge_line_from_shared_edge(model, shared_ec)
                if not hinge:
                    visited.add(neighbor_fid)
                    visited.add(next_planar)
                    continue

                # Get normal of next planar face
                next_normal_raw = model.face_normal_outward(next_planar)
                if not next_normal_raw:
                    visited.add(neighbor_fid)
                    visited.add(next_planar)
                    continue

                # Compute bend angle from the two planar face normals
                bend_angle = _bend_angle_from_normals(cur_normal, next_normal_raw)

                # Only process bends with a meaningful angle (skip coplanar / anti-parallel)
                if bend_angle < math.radians(5) or bend_angle > math.radians(175):
                    visited.add(neighbor_fid)
                    visited.add(next_planar)
                    continue

                # Bend radius = inner radius — find min radius among paired cylinders at same axis
                radius = cyl_data['radius']
                cyl_ax = cyl_data['axis']
                cyl_orig = cyl_data['origin']
                for other_cid, other_c in model.cylinders.items():
                    if other_c['radius'] >= radius:
                        continue
                    if abs(abs(_dot(other_c['axis'], cyl_ax)) - 1.0) > 0.05:
                        continue
                    d = _sub(other_c['origin'], cyl_orig)
                    perp = _sub(d, _scale(cyl_ax, _dot(d, cyl_ax)))
                    if _len(perp) < 2.0:
                        radius = other_c['radius']
                        break
                ba = bend_angle * (radius + k_factor * thickness)

                bend_specs.append({
                    'angle_deg': round(math.degrees(bend_angle), 1),
                    'radius_mm': round(radius, 2),
                    'ba_mm': round(ba, 2),
                })

                # Unfolding transform for next_planar:
                #   1. Apply cur_transform (accumulated from previous steps)
                #   2. Rotate around the B-side hinge to flatten face B
                #   3. Translate by BA to separate panels in the flat pattern

                hinge_pt, hinge_dir = hinge
                # Transform hinge into the current accumulated flat space
                hinge_pt_t = cur_transform(hinge_pt)
                hinge_pt2_t = cur_transform(_add(hinge_pt, hinge_dir))
                hinge_dir_t = _norm(_sub(hinge_pt2_t, hinge_pt_t))

                # Compute the normal of next_planar after applying cur_transform
                # (but before the additional rotation)
                n_B_pt_t = cur_transform(_add(hinge_pt, _scale(next_normal_raw, 10.0)))
                n_B_t = _norm(_sub(n_B_pt_t, hinge_pt_t))

                # Rotation angle = bend_angle, direction chosen so n_B rotates to cur_normal
                # sign = dot(cross(n_B_t, cur_normal), hinge_dir_t)
                cross_n = _cross(n_B_t, cur_normal)
                rot_sign = 1.0 if _dot(cross_n, hinge_dir_t) >= 0 else -1.0
                rot_a = rot_sign * bend_angle

                # BA offset direction: perpendicular to hinge in the flat plane,
                # pointing from the B-hinge toward where face A extends (away from B)
                # = cross(hinge_dir_t, cur_normal), pointing toward A
                off_dir_raw = _norm(_cross(hinge_dir_t, cur_normal))
                # Sample a point on cur_fid away from hinge to find correct direction
                sample_pt = cur_transform(_add(hinge_pt, _scale(cur_normal, 0.0)))
                # A point clearly in face A: base_origin + some offset in A's plane
                a_sample = cur_transform(base_origin)
                if _dot(_sub(a_sample, hinge_pt_t), off_dir_raw) < 0:
                    off_dir_raw = _scale(off_dir_raw, -1.0)
                # BA goes away from A (in the -off_dir direction from B-hinge)
                off_dir = _scale(off_dir_raw, -1.0)

                prev_t = cur_transform
                rot_pt = hinge_pt_t
                rot_axis = hinge_dir_t
                ba_offset = ba

                def make_transform(prev, rpt, rax, ra, ba_off, off_d):
                    def t(p):
                        p1 = prev(p)
                        p2 = _rotate_point(p1, rax, ra, rpt)
                        p3 = _add(p2, _scale(off_d, ba_off))
                        return p3
                    return t

                new_transform = make_transform(prev_t, rot_pt, rot_axis, rot_a, ba_offset, off_dir)
                # Track updated normal for the next iteration
                n_test = new_transform(_add(base_origin, next_normal_raw))
                n_base_t = new_transform(base_origin)
                new_normal = _norm(_sub(n_test, n_base_t))

                transforms[next_planar] = new_transform
                normals_3d[next_planar] = new_normal
                visited.add(neighbor_fid)
                visited.add(next_planar)
                queue.append(next_planar)

            elif kind == 'plane' and neighbor_fid in plane_faces:
                # Directly adjacent planar face — only inherit transform if coplanar
                n_neigh = model.face_normal_outward(neighbor_fid)
                if n_neigh and abs(abs(_dot(cur_normal, n_neigh)) - 1.0) < 0.05:
                    if neighbor_fid not in visited:
                        transforms[neighbor_fid] = cur_transform
                        normals_3d[neighbor_fid] = cur_normal
                        visited.add(neighbor_fid)
                        queue.append(neighbor_fid)

    # Project all visited planar faces to 2D
    flat_outlines = []
    bend_lines_2d = []
    u2d = base_ref
    v2d = base_vdir

    for fid, tf in transforms.items():
        polys = model.face_3d_outline(fid)
        for poly in polys:
            pts2d = [_project2d(tf(pt), base_origin, u2d, v2d) for pt in poly]
            if len(pts2d) < 2:
                continue
            # Skip degenerate paths where all points are the same
            xs = [p[0] for p in pts2d]; ys = [p[1] for p in pts2d]
            if (max(xs)-min(xs) < 0.01 and max(ys)-min(ys) < 0.01):
                continue
            flat_outlines.append(pts2d)

    # Collect bend lines: one per real bend, using the hinge edge from the
    # smaller-transform-side face so each bend is added only once.
    seen_bend_cyls: set = set()
    for cur_fid, tf in transforms.items():
        for (cyl_fid, shared_ec) in adj.get(cur_fid, []):
            if model.face_kind(cyl_fid) != 'cylinder':
                continue
            if cyl_fid in seen_bend_cyls:
                continue
            # Only emit bend lines for cylinders that connect two transformed faces
            cyl_adj_transformed = [
                (ff, ec) for (ff, ec) in adj.get(cyl_fid, [])
                if ff in transforms and ff != cur_fid
            ]
            if not cyl_adj_transformed:
                continue
            seen_bend_cyls.add(cyl_fid)
            # Use the hinge on the current (base) side
            hinge = _hinge_line_from_shared_edge(model, shared_ec)
            if not hinge:
                continue
            p0, hd = hinge
            # Extend the hinge 500mm in each direction to cover the full part width
            p0e = _add(p0, _scale(hd, -500.0))
            p1e = _add(p0, _scale(hd, 500.0))
            p0_2d = _project2d(tf(p0e), base_origin, u2d, v2d)
            p1_2d = _project2d(tf(p1e), base_origin, u2d, v2d)
            bend_lines_2d.append([p0_2d, p1_2d])

    # Bounding box
    all_pts = [pt for outline in flat_outlines for pt in outline]
    if all_pts:
        xs = [p[0] for p in all_pts]; ys = [p[1] for p in all_pts]
        bbox = (max(xs)-min(xs), max(ys)-min(ys))
        x_off, y_off = -min(xs) + 5.0, -min(ys) + 5.0
    else:
        bbox = (0, 0)
        x_off, y_off = 5.0, 5.0

    svg = _render_svg(flat_outlines, bend_lines_2d, bbox, x_off, y_off, thickness, bend_specs)

    return {
        'thickness_mm': thickness,
        'bends': bend_specs,
        'flat_outlines': flat_outlines,
        'bend_lines': bend_lines_2d,
        'bbox_mm': bbox,
        'svg': svg,
    }


# ─── SVG renderer ──────────────────────────────────────────────────────────────

def _render_svg(
    outlines: List[List[Vec2]],
    bend_lines: List[List[Vec2]],
    bbox: Tuple[float, float],
    x_off: float,
    y_off: float,
    thickness: float,
    bends: List[dict],
    margin: float = 10.0,
) -> str:
    W = bbox[0] + 2 * margin
    H = bbox[1] + 2 * margin

    def tx(x): return round(x + x_off + margin - 5.0, 2)
    def ty(y): return round(H - (y + y_off + margin - 5.0), 2)  # flip Y

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.1f}mm" height="{H:.1f}mm" '
        f'viewBox="0 0 {W:.1f} {H:.1f}">',
        f'<rect width="{W:.1f}" height="{H:.1f}" fill="white"/>',
        '<g id="outlines" fill="none" stroke="black" stroke-width="0.3">',
    ]

    for outline in outlines:
        if len(outline) < 2:
            continue
        d = 'M ' + ' L '.join(f'{tx(p[0])},{ty(p[1])}' for p in outline) + ' Z'
        parts.append(f'  <path d="{d}"/>')

    parts.append('</g>')
    parts.append('<g id="bendlines" fill="none" stroke="blue" stroke-width="0.3" '
                 'stroke-dasharray="2,1">')

    # Clip bend lines to the bounding box (extend across full width/height)
    for bl in bend_lines:
        if len(bl) < 2:
            continue
        x1, y1 = tx(bl[0][0]), ty(bl[0][1])
        x2, y2 = tx(bl[1][0]), ty(bl[1][1])
        parts.append(f'  <line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}"/>')

    parts.append('</g>')

    # Dimension annotations
    parts.append('<g id="dims" font-family="sans-serif" font-size="3" fill="#333">')
    parts.append(f'  <text x="{W/2:.1f}" y="{H-2:.1f}" text-anchor="middle">'
                 f'Flat: {bbox[0]:.1f} x {bbox[1]:.1f} mm  t={thickness:.1f}mm</text>')

    for i, b in enumerate(bends):
        y_pos = 5.0 + i * 4.5
        parts.append(f'  <text x="{W-2:.1f}" y="{y_pos:.1f}" text-anchor="end" '
                     f'fill="blue">Bend {i+1}: R={b["radius_mm"]:.1f} '
                     f'{b["angle_deg"]:.0f}° BA={b["ba_mm"]:.1f}mm</text>')

    parts.append('</g>')
    parts.append('</svg>')
    return '\n'.join(parts)


# ─── API entry point ───────────────────────────────────────────────────────────

def flat_pattern_svg(path: str, k_factor: float = 0.33) -> bytes:
    """Return flat pattern as SVG bytes."""
    result = unfold(path, k_factor=k_factor)
    return result['svg'].encode('utf-8')


# ─── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys, json
    path = sys.argv[1] if len(sys.argv) > 1 else 'step/angle_bracket.step'
    k = float(sys.argv[2]) if len(sys.argv) > 2 else 0.33
    result = unfold(path, k_factor=k)
    print(f"Thickness: {result['thickness_mm']} mm")
    print(f"Bends ({len(result['bends'])}):")
    for b in result['bends']:
        print(f"  angle={b['angle_deg']}deg  R={b['radius_mm']}mm  BA={b['ba_mm']}mm")
    w, h = result['bbox_mm']
    print(f"Flat blank: {w:.1f} x {h:.1f} mm")
    svg_path = path.replace('.step', '_flat.svg').replace('.stp', '_flat.svg')
    with open(svg_path, 'w', encoding='utf-8') as f:
        f.write(result['svg'])
    print(f"SVG written to {svg_path}")
