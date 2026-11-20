"""Root-authored geometric shadow-camera diagrams, never captured buffers."""
from itertools import product


def shadow_geometry(Drawing):
    blue, gold, green, red, muted = '#65baff', '#ffd36f', '#6ce1b5', '#ff8c91', '#a7b9d3'
    d = Drawing('Choose a shadow camera from the light',
                'Directional rays use orthographic views, spot lights use a perspective cone, and a point light covers directions with six perspective cube faces.')
    d.line(410, 100, 410, 525, '#293c50', 2)
    d.line(790, 100, 790, 525, '#293c50', 2)
    d.text(60, 115, 'Directional: parallel rays', 24, gold)
    d.text(445, 115, 'Spot: a perspective cone', 24, gold)
    d.text(825, 115, 'Point: six views', 24, gold)
    # Parallel rays and nested light-space coverage, shown as a planar schematic.
    for start in (85, 165, 245):
        d.line(start, 175, start + 75, 360, gold, 3, True)
    d.rect(70, 280, 290, 135, blue, 'none', 0)
    d.rect(125, 305, 180, 85, blue, 'none', 0)
    d.line(60, 420, 370, 420, muted, 4)
    d.text(65, 470, 'Orthographic clipmaps', 22, blue)
    d.text(65, 502, 'Nested coverage around the view', 19, muted)
    # A 2D section through a perspective shadow camera, not a cone of shadow rays.
    d.circle(600, 180, 10, gold, gold)
    d.line(600, 190, 475, 420, gold, 3)
    d.line(600, 190, 725, 420, gold, 3)
    d.line(600, 195, 600, 410, gold, 2, True, True)
    d.line(573, 240, 627, 240, blue, 3)
    d.line(475, 420, 725, 420, blue, 3)
    d.text(640, 244, 'near', 18, blue)
    d.text(680, 446, 'far', 18, blue)
    d.text(450, 470, 'Perspective projection', 22, blue)
    d.text(450, 502, 'Outer cone bounds the field', 19, muted)
    # Project a real 3D cube so the six face directions remain distinct.
    def p(v):
        x, y, z = v
        return 985 + 65*(x - .7*y), 315 + 50*(.45*x + .45*y - z)
    vertices = list(product((-1, 1), repeat=3))
    for a in vertices:
        for axis in range(3):
            if a[axis] != -1:
                continue
            b = list(a); b[axis] = 1
            hidden = all(a[i] < 0 for i in range(3) if i != axis)
            d.line(*p(a), *p(b), muted, 2, dash=hidden)
    for axis in range(3):
        for sign in (-1, 1):
            v = [0, 0, 0]; v[axis] = sign
            d.line(*p((0, 0, 0)), *p(v), gold, 2, True, sign < 0)
    d.circle(*p((0, 0, 0)), 7, gold, gold)
    d.text(825, 470, 'Directions ±X, ±Y, ±Z', 22, blue)
    d.text(825, 502, 'One perspective face per direction', 18, muted)
    projection = d.finish()

    d = Drawing('An off-screen caster can shadow a visible receiver',
                'A point light and an intervening caster both lie outside the primary camera frustum, yet the caster blocks the light connection to a visible surface point.')
    d.poly([(600, 510), (450, 110), (1050, 110)], blue, '#142840')
    d.text(695, 155, 'camera-visible region', 22, blue)
    # The light-to-receiver segment intersects the caster at (400,239.66).
    d.line(132, 163.4, 387, 236, gold, 4, True)
    d.line(414, 243.6, 700, 325, red, 3, True, True)
    d.rect(388, 205, 25, 75, red, '#394255', 0)
    d.circle(120, 160, 12, gold, gold)
    d.circle(700, 325, 8, green, green)
    d.line(600, 500, 696, 333, green, 2, True, True)
    d.circle(600, 510, 8, blue, blue)
    d.text(60, 115, 'off-screen light', 23, gold)
    d.text(240, 190, 'off-screen caster', 23, red)
    d.line(700, 278, 700, 310, muted, 1.5)
    d.text(630, 230, 'visible receiver', 23, green)
    d.text(630, 262, 'blocked light connection', 18, red)
    d.text(630, 503, 'primary camera', 22, blue)
    d.lines(60, 420, ['Receiver demand selects light-space pages.',
                     'Light-space culling selects their casters.',
                     'These are different visibility questions.'], size=20)
    return {'shadow-projections.svg': projection, 'shadow-offscreen.svg': d.finish()}
