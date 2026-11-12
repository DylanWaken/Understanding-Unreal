"""Topology regressions for the actual QEM collapse loop, not a mock predicate."""
import unittest
import numpy as np
from run import simplify,edge_faces


def closed_sphere_invariants(vertices,faces):
    canonical={tuple(sorted(map(int,t))) for t in faces}
    edges=edge_faces(faces)
    return {
        'unique_faces':len(canonical)==len(faces),
        'two_faces_per_edge':all(len(ts)==2 for ts in edges.values()),
        'euler_characteristic':len(np.unique(faces))-len(edges)+len(faces),
    }


class CollapseTopologyTests(unittest.TestCase):
    def test_tetrahedron_cannot_become_opposite_winding_duplicate_faces(self):
        # This thin tetrahedron previously passed the neighbor-only link test and
        # normal test, becoming the invalid pair [0,2,1] / [0,1,2].
        vertices=np.array([[0,1,.1],[0,1,-.1],[0,0,0],[1,0,0]],dtype=float)
        faces=np.array([[0,2,1],[0,1,3],[0,3,2],[1,2,3]])
        v,f,_,_,stats=simplify(vertices,faces,np.arange(4),target=2)
        self.assertEqual(len(f),4)
        self.assertEqual(stats['collapses'],0)
        self.assertEqual(closed_sphere_invariants(v,f),{
            'unique_faces':True,'two_faces_per_edge':True,'euler_characteristic':2})

    def test_valid_octahedron_collapse_still_simplifies_a_closed_sphere(self):
        vertices=np.array([[1,0,0],[-1,0,0],[0,1,0],[0,-1,0],[0,0,1],[0,0,-1]],dtype=float)
        faces=np.array([[0,2,4],[2,1,4],[1,3,4],[3,0,4],
                        [2,0,5],[1,2,5],[3,1,5],[0,3,5]])
        v,f,_,_,stats=simplify(vertices,faces,np.arange(6),target=2)
        self.assertGreater(stats['collapses'],0)
        self.assertLess(len(f),8)
        self.assertGreaterEqual(len(f),4)
        self.assertEqual(closed_sphere_invariants(v,f),{
            'unique_faces':True,'two_faces_per_edge':True,'euler_characteristic':2})


if __name__=='__main__': unittest.main()
