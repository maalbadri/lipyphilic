# -*- Mode: python; tab-width: 4; indent-tabs-mode:nil; coding:utf-8 -*-
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
# lipyphilic --- lipyphilic.readthedocs.io
#
# Released under the GNU Public Licence, v2 or any higher version
#

"""Assign leaflets --- :mod:`lipyphilic.lib.assign_leaflets`
============================================================

:Author: Paul Smith
:Year: 2021
:Copyright: GNU Public License v2

This module provides methods for assigning lipids to leaflets in a bilayer.

The :class:`AssignLeaflets` assigns each lipid to a leaflet based on the
distance in *z* to the midpoint of the bilayer.

Input
------

Required:
  - *universe* : an MDAnalysis Universe object
  - *lipid_sel*: atom for all lipids in the bilayer

Options:
  - *midplane_sel* : atom selection for lipid that may occupy the midplane
  - *midplane_cutoff* : atoms within this distance from the midpoint are considered
        to be the midplane
  - *n_bins* : split the membrane into *n_bins \\* n_bins* patches, and calculate
        local membrane midpoints for each patch

Output
------

  - *leaflets* : leaflet to which each lipid is assigned at each frame
  
Leaflet data are returned in a :class:`numpy.ndarray`, where each column corresponds
to an individual lipid and each columns corresponds to an individual frame. The
results are accessible via the `AssignLeaflets.leaflets` attribute.

Example usage of :class:`AssignLeaflets`
----------------------------------------

An MDAnalysis Universe must first be created before using AssignLeaflets::

  import MDAnalysis as mda
  from lipyphilic.lib.assign_leaflets import AssignLeaflets

  u = MDAnalysis.Universe(tpr, trajectory)

If we have used the MARTINI forcefield to study phospholipid/cholesterol mixture,
we can assign lipids and cholesterol the upper and lower as follows::

  leaflets = AssignLeaflets(
    universe=u,
    lipid_sel="name GL1 GL2 ROH"
  )
  
We then select which frames of the trajectory to analyse (`None` will use every
frame) and choose to display a progress bar (`verbose=True`)::
  
  leaflets.run(
    start=None,
    stop=None,
    step=None,
    verbose=True
  )
  
The results are then available in the `leaflets.leaflets` attribute in a
`numpy.ndarray`. Each row corresponds to an individual lipid and each column
to an individual frame, i.e `leaflets.leaflets[i, j]` contains the leaflet
membership of lipid *i* at frame *j*. Lipid *i*, at frame *j*, is in the upper
leaflet if `leaflets.leaflets[i, j]==1` and in the lower leaflet if
`leaflets.leaflets[i, j]==-1`.

The above example will assign every lipid (including sterols) to either the upper
or lower leaflet. To allow cholesterol to be in the midplane, we can provide
a `midplane_sel` and `midplane_cutoff` to `AssignLeaflets`::

  leaflets = AssignLeaflets(
    universe=u,
    lipid_sel="name GL1 GL2 ROH",
    midplane_sel="resname CHOL and name ROH C2",
    midplane_cutoff=12.0
  )
  
A cholesterol molecule that has both its **ROH** and **C2** atoms within *12* Å of
membrane midpoint will be assigned to the midplane, i.e. for cholesterol *i*
at frame *j* that is in the midplane, `leaflets.leaflets[i, j]==-0`.

The first two examples compute a global membrane midpoint based on all the atoms
of the lipids in the. Lipids are then assigned a leaflet based on their distance
in *z* to this midpoint. This is okay for planar bilayers, but can lead to incorrect
leaflet classification in membranes with large undulations. If your bilayer has
large undulations, `AssignLeaflets` account for this by creating a grid in *xy*
patches of your membrane, calculating the local membrane midpoint in each patch,
the assign leaflet membership based on distance in *z* to the local membrane
midpoint. This is done through use of `n_bins`::

  leaflets = AssignLeaflets(
    universe=u,
    lipid_sel="name GL1 GL2 ROH",
    midplane_sel="resname CHOL and name ROH C2",
    midplane_cutoff=12.0,
    n_bins=10
  )
  
  In this example, the membrane will be split into a *10 x 10* grid and a lipid
  assigned a leaflet based on the distance to the midpoint of the patch the lipid
  is in.

The class and its methods
-------------------------

.. autoclass:: AssignLeaflets
   :members:
"""

import numpy as np
import scipy.stats
from MDAnalysis.analysis.base import AnalysisBase


class AssignLeaflets(AnalysisBase):
    """
    Assign lipids in a bilayer to the upper leaflet, lower leaflet, or midplane.
    """

    def __init__(self, universe,
                 lipid_sel=None,
                 midplane_sel=None, midplane_cutoff=0.0,
                 n_bins=1):
        """Set up parameters for finding lipids in a leaflet.

        Parameters
        ----------
        universe : Universe
            MDAnalysis Universe object
        lipid_sel : str
            Selection string for the lipids in a membrane. The selection
            should cover **all** residues in the membrane, including cholesterol
        midplane_sel :  str
            Selection string for residues that may be midplane. Any residues not
            in this selection will be assigned to a leaflet regardless of its
            proximity to the midplane.
            The default is `None`, in which case all lipids will be assigned to
            either the upper or lower leaflet.
        midplane_cutoff : float
            Minimum distance in *z* an atom must be from the midplane to be assigned
            to a leaflet rather than the midplane. The default is `0`, in which case
            all lipids will be assigned to either the upper or lower leaflet.
        n_bins : int
            Number of bins in *x* and *y* to use to create a grid of membrane patches.
            Local membrane midpoints are computed for each patch, and lipids assigned
            a leaflet based on the distance to their local membrane midpoint. The
            default is `1`, which is equivalent to computing a single global
            midpoint.
            
        Note
        ----

        Typically, `midplane_sel` should select only sterols as other lipids have
        flip-flop rates that are currently unaccessible with MD simulations.

        """
        
        self.u = universe
        self._trajectory = self.u.trajectory
        self.membrane = self.u.select_atoms(lipid_sel, updating=False)
        self.potential_midplane = self.u.select_atoms(midplane_sel, updating=False) if midplane_sel else None
        self.midplane_cutoff = midplane_cutoff
        self.n_bins = n_bins
        self.leaflets = None

    def _prepare(self):
        
        # Output array
        self.leaflets = np.full(
            (self.membrane.n_residues, self.n_frames),
            fill_value=0,
            dtype=np.int8  # smallest sized `np.int` is 1 byte, still 8 times smaller than using `int`
        )

    def _single_frame(self):
        
        # Atoms must be wrapped before creating a lateral grid of the membrane
        self.membrane.wrap(inplace=True)

        # Find the midpoint of the bilayer as a function of (x,y), using
        # `n_bins` grid points in each dimensions
        # Use all atoms in the membrane to get better statistics
        bins = np.linspace(0.0, self._ts.dimensions[0], self.n_bins + 1)
        
        memb_midpoint_xy = scipy.stats.binned_statistic_2d(
            x=self.membrane.residues.atoms.positions[:, 0],
            y=self.membrane.residues.atoms.positions[:, 1],
            values=self.membrane.residues.atoms.positions[:, 2],
            statistic="mean",
            bins=bins,
            expand_binnumbers=True
        )
        
        # get the binnumbers for each lipid
        lipid_x_bins, lipid_y_bins = scipy.stats.binned_statistic_2d(
            x=self.membrane.positions[:, 0],
            y=self.membrane.positions[:, 1],
            values=self.membrane.positions[:, 2],
            statistic="mean",
            bins=bins,
            expand_binnumbers=True
        ).binnumber -1  # These were bin numbers, now bin indices  # noqa: E225
        
        # Assign leaflets
        upper_leaflet = self.membrane[
            self.membrane.positions[:, 2] >
            (memb_midpoint_xy.statistic[lipid_x_bins, lipid_y_bins] +
             self.midplane_cutoff)
        ]
        self.leaflets[
            np.in1d(self.membrane.residues.resindices, upper_leaflet.residues.resindices),
            self._frame_index
        ] = 1
        
        lower_leaflet = self.membrane[
            self.membrane.positions[:, 2] <
            (memb_midpoint_xy.statistic[lipid_x_bins, lipid_y_bins] -
             self.midplane_cutoff)
        ]
        self.leaflets[
            np.in1d(self.membrane.residues.resindices, lower_leaflet.residues.resindices),
            self._frame_index
        ] = -1
        
        # if necessary, check for midplane residues
        if (self.potential_midplane is not None) and self.midplane_cutoff > 0.0:
            
            # TODO: Move this code block into a method
            self.potential_midplane.wrap(inplace=True)

            midplane_x_bins, midplane_y_bins = scipy.stats.binned_statistic_2d(
                x=self.potential_midplane.positions[:, 0],
                y=self.potential_midplane.positions[:, 1],
                values=self.potential_midplane.positions[:, 2],
                statistic="mean",
                bins=bins,
                expand_binnumbers=True
            ).binnumber -1  # These were bin numbers, now bin indices  # noqa: E225
            
            # First assume they're all midplane
            # Then find residues that have at least one atom further than
            # `midplane_cutoff` from the local midplane
            midplane_mask = np.full(self.potential_midplane.n_residues, fill_value=True, dtype=bool)

            not_midplane = np.abs(
                self.potential_midplane.positions[:, 2] - memb_midpoint_xy.statistic[midplane_x_bins, midplane_y_bins]
            ) > self.midplane_cutoff

            # These residues have at least one atom in `potential_midplane`
            # that is more the `midplane_cutoff` from the local midplane
            midplane_mask[
                np.in1d(self.potential_midplane.residues.resindices, self.potential_midplane[not_midplane].resindices),
            ] = False

            midplane_residues = self.potential_midplane.residues[midplane_mask]

            # Assign midplane
            self.leaflets[
                np.in1d(self.membrane.residues.resindices, midplane_residues.resindices),
                self._frame_index
            ] = 0