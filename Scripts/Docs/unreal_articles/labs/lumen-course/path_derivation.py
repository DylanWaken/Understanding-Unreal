"""Original path-space teaching diagrams, authored with the chapter.

These explain algebraic structure, not measured Unreal data. Text is emitted
after geometry by Drawing so labels remain legible and selectable in the SVG.
"""


def path_derivation(Drawing):
    blue, gold, green, purple = '#65baff', '#ffd36f', '#6ce1b5', '#c4a0ff'
    fg, muted = '#eef4ff', '#a7b9d3'
    output = {}
    d = Drawing('One substitution adds one scattering event',
                'Each row is a full integral over its vertex positions. Light flows '
                'left to right. Each added scattering vertex adds an area variable, '
                'a BSDF factor, and a geometry factor. A remainder retains longer paths.')
    for k, y, xs in [(1, 160, [160, 1030]),
                     (2, 300, [160, 595, 1030]),
                     (3, 440, [160, 450, 740, 1030])]:
        d.text(40, y-40, f'k = {k}  ·  {k-1} scattering '+('event' if k == 2 else 'events'), 21, muted)
        for i, (x0, x1) in enumerate(zip(xs, xs[1:])):
            d.line(x0+12, y, x1-14, y, gold, 3, True)
            d.text((x0+x1)/2, y-16, ['G₀₁', 'G₁₂', 'G₂₃'][i], 22, gold, 'middle')
        for i, x in enumerate(xs):
            color = gold if i == 0 else purple if i == k else green
            d.circle(x, y, 10, color, color)
            d.text(x, y+34, ['x₀', 'x₁', 'x₂', 'x₃'][i], 22, color, 'middle')
            label = 'emission Lₑ' if i == 0 else 'sensor Wₑ' if i == k else 'scatter fₛ'
            d.text(x, y+62, label, 21, color, 'middle')
    d.text(40, 548, 'Integrate each row over all its vertices. Sum the rows; retain the unresolved tail.', 22, fg)
    output['path-expansion.svg'] = d.finish()

    d = Drawing('The hit density carries the same geometric falloff',
                'For a visible ray hit, geometry is starting cosine times target cosine '
                'over distance squared. The area PDF is the solid-angle PDF times target '
                'cosine over distance squared. Dividing cancels the target cosine and '
                'distance factor, leaving starting cosine divided by solid-angle PDF.')
    d.text(40, 99, 'cₓ = cosine at the starting point     cᵧ = cosine at the hit     r = distance     V = 1', 22, muted)
    for x, title, color in [(40, 'Geometry in the integrand', gold),
                             (440, 'Density of the sampled hit', blue),
                             (840, 'Ratio in the estimator', purple)]:
        d.rect(x, 140, 320, 250, color, '#18283e', 8)
        d.text(x+18, 180, title, 21, color)
    d.text(65, 236, 'G = cₓ ×', 28, gold)
    d.rect(195, 200, 140, 60, green, '#133c36')
    d.text(265, 239, 'cᵧ / r²', 28, green, 'middle')
    d.lines(65, 305, ['Two projected areas,', 'one inverse square.'], fg, 22)
    d.text(465, 236, 'pₐ = pσ ×', 28, blue)
    d.rect(605, 200, 130, 60, green, '#133c36')
    d.text(670, 239, 'cᵧ / r²', 28, green, 'middle')
    d.lines(465, 305, ['A direction density', 'converted to area.'], fg, 22)
    d.text(865, 239, 'G / pₐ = cₓ / pσ', 28, purple)
    d.lines(865, 305, ['The green factors', 'cancel exactly.'], fg, 22)
    d.line(370, 265, 428, 265, muted, 3, True)
    d.line(770, 265, 828, 265, muted, 3, True)
    d.text(600, 457, 'Multiply by the BSDF:   βnew = βold × fₛ × cₓ / pσ', 29, fg, 'middle')
    d.text(600, 514, 'Distance reduces the chance of hitting the patch. The geometry factor cancels in the hit weight.', 22, muted, 'middle')
    output['path-density.svg'] = d.finish()

    d = Drawing('Different proposals, one measured integral',
                'A two-cell example: the target integral has cell contributions 0.8 and '
                '0.2. Light sampling has probabilities 0.9 and 0.1; BSDF sampling 0.2 '
                'and 0.8; a half-and-half mixture 0.55 and 0.45. All rows sum to one. '
                'The first gives the target contributions. The others give sampling probabilities.')
    d.text(40, 105, 'Blue: cell 1     Green: cell 2     Target contributions and probabilities are distinct.', 22, muted)
    for y, title, left, right in [(160, 'Target contribution', .8, .2),
                                 (245, 'Light probability', .9, .1),
                                 (330, 'BSDF probability', .2, .8),
                                 (415, '50/50 mixture', .55, .45)]:
        d.text(40, y+28, title, 23, fg)
        for start, width, value, color in [(310, 800*left, left, blue),
                                           (310+800*left, 800*right, right, green)]:
            d.rect(start, y, width, 43, color, color, 0)
            d.text(start+width/2, y+29, f'{value:g}', 23, '#101b2d', 'middle')
    d.text(600, 522, 'A sampled cell returns its contribution divided by its sampling probability.', 23, fg, 'middle')
    output['mis-strategies.svg'] = d.finish()
    return output
