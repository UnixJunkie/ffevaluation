# (c) 2015-2018 Acellera Ltd http://www.acellera.com
# All Rights Reserved
# Distributed under HTMD Software License Agreement
# No redistribution in whole or part
#
import logging
from math import sqrt, acos, radians, cos, sin, pi
import numpy as np
from numba import jit
from scipy import constants as const
from ffevaluation.numbautil import (
    dihedralAngleFull,
    wrapBondedDistance,
    wrapDistance,
    cross,
    dot,
)

logger = logging.getLogger(__name__)


def loadParameters(fname):
    """Convenience method for reading parameter files with parmed

    Parameters
    ----------
    fname : str
        Parameter file name

    Returns
    -------
    prm : ParameterSet
        A parmed ParameterSet object

    Examples
    --------
    >>> from ffevaluation.home import home
    >>> from os.path import join
    >>> prm = loadParameters(join(home(dataDir='thrombin-ligand-amber'), 'structure.prmtop'))
    """
    import parmed

    prm = None
    if fname.endswith(".prm"):
        try:
            prm = parmed.charmm.CharmmParameterSet(fname)
        except Exception as e:
            print(
                "Failed to read {} as CHARMM parameters. Attempting with AMBER prmtop reader".format(
                    fname
                )
            )
            try:
                struct = parmed.amber.AmberParm(fname)
                prm = parmed.amber.AmberParameterSet.from_structure(struct)
            except Exception as e2:
                print("Failed to read {} due to errors {} {}".format(fname, e, e2))
    elif fname.endswith(".prmtop"):
        from parmed import TrackedList

        struct = parmed.amber.AmberParm(fname)
        # Clear CMAPs. They cause parsing issues sometimes and we don't use them here
        struct.cmaps = TrackedList()
        struct.cmap_types = TrackedList()
        prm = parmed.amber.AmberParameterSet.from_structure(struct)
    elif fname.endswith(".frcmod"):
        prm = parmed.amber.AmberParameterSet(fname)

    if prm is None:
        raise RuntimeError(
            "Extension of file {} not recognized. Report issue on HTMD issue tracker.".format(
                fname
            )
        )

    return prm


class FFEvaluate:
    @staticmethod
    def formatEnergies(energies):
        """Formats the energies into a dictionary.

        Parameters
        ----------
        energies : np.ndarray
            An energy array returned by `calculate`

        Returns
        -------
        energiesdict : dict
            A dictionary containing the energies
        """
        energies = energies.squeeze()
        return {
            "angle": energies[3],
            "bond": energies[0],
            "dihedral": energies[4],
            "elec": energies[2],
            "improper": energies[5],
            "vdw": energies[1],
            "total": energies.sum(axis=0),
        }

    def __init__(
        self, mol, prm, betweensets=None, cutoff=0, rfa=False, solventDielectric=78.5
    ):
        """Evaluates energies and forces of the forcefield for a given Molecule

        Parameters
        ----------
        mol : :class:`Molecule <moleculekit.molecule.Molecule>` object
            A Molecule object. Can contain multiple frames.
        prm : :class:`ParameterSet <parmed.ParameterSet>` object
            Forcefield parameters.
        betweensets : tuple of strings
            Only calculate energies between two sets of atoms given as atomselect strings.
            Only computes LJ and electrostatics.
        cutoff : float
            If set to a value != 0 it will only calculate LJ, electrostatics and bond energies for atoms which are closer
            than the threshold
        rfa : bool
            Use with `cutoff` to enable the reaction field approximation for scaling of the electrostatics up to the cutoff.
            Uses the value of `solventDielectric` to model everything beyond the cutoff distance as solvent with uniform
            dielectric.
        solventDielectric : float
            Used together with `cutoff` and `rfa`

        Examples
        --------
        >>> from ffevaluation.test_ffevaluate import fixParameters, drawForce
        >>> from moleculekit.molecule import Molecule
        >>> import parmed
        >>> mol = Molecule('./ffevaluation/test-data/waterbox/structure.psf')
        >>> mol.read('./ffevaluation/test-data/waterbox/output.xtc')
        >>> prm = loadParameters(fixParameters('./ffevaluation/test-data/waterbox/parameters.prm'))
        >>> ffev = FFEvaluate(mol, prm, betweensets=('resname SOD', 'water'))
        >>> energies, forces, atmnrg = ffev.calculate(mol.coords)

        You can visualize the force vectors in VMD
        >>> mol.view()
        >>> for cc, ff in zip(mol.coords[:, :, 0], forces[:, :, 0]):
        >>>     drawForce(cc, ff)

        Amber style
        >>> prm = loadParameters('structure.prmtop')
        >>> ffev = FFEvaluate(mol, prm, betweensets=('resname SOD', 'water'))
        >>> energies, forces, atmnrg = ffev.calculate(mol.coords)
        """
        mol = mol.copy()
        setA, setB = calculateSets(mol, betweensets)

        args = list(init(mol, prm))
        args.append(setA)
        args.append(setB)
        args.append(cutoff)
        args.append(rfa)
        args.append(solventDielectric)
        self._args = args

    def calculateEnergies(self, coords, box=None, formatted=True):
        """Utility method which calls `calculate` to calculate energies and returns them.

        Parameters
        ----------
        coords : np.ndarray
            A (natoms, 3, nframes) shaped coordinates array.
        box : np.ndarray
            A (3, nframes) shaped periodic box dimensions array.
        formatted : bool
            If True it will return a dictionary of the energies. If False it returns an array of energies as described in
            calculate.

        Returns
        -------
        energies : dict or np.ndarray
            The energies as a dictionary or np.array depending on option `formatted`
        """
        energies, _, _ = self.calculate(coords, box)
        if formatted:
            energies = self.formatEnergies(energies)
        return energies

    def calculate(self, coords, box=None):
        """Calculates energies, forces and individual atom energies for given coordinates and periodic box.

        Parameters
        ----------
        coords : np.ndarray
            A (natoms, 3, nframes) shaped coordinates array.
        box : np.ndarray
            A (3, nframes) shaped periodic box dimensions array.

        Returns
        -------
        energies : np.ndarray
            A (6, nframes) shaped matrix containing the individual energy components of each simulation frame.
            Rows correspond to the following energies 0: bond 1: LJ 2: Electrostatic 3: angle 4: dihedral 5: improper
        forces : np.ndarray
            A (natoms, 3, nframes) shaped matrix containing the total force on each atom for each simulation frame.
        atmnrg : np.ndarray
            A (natoms, 6, nframes) shaped matrix containing the approximate potential energy components of each atom at each
            simulation frame. The 6 indexes are the same as in the `energies` return argument.
        """
        if coords.ndim == 2:
            coords = coords[:, :, np.newaxis].copy()

        if box is None:
            box = np.zeros((3, coords.shape[2]), dtype=np.float32)

        if box.shape[0] != 3 or box.shape[1] != coords.shape[2]:
            raise ValueError(
                "Box dimensions have to be (3, numFrames), your Molecule has box of shape {}".format(
                    box.shape
                )
            )

        energies, forces, atmnrg = _ffevaluate(
            coords.astype(np.float32), box.astype(np.float32), *self._args
        )
        return energies, forces, atmnrg


def nestedListToArray(nl, dtype, default=1):
    if len(nl) == 0:
        return np.ones((1, 1), dtype=dtype) * default
    dim = list()
    dim.append(len(nl))
    dim.append(max([len(x) for x in nl]))
    alllens = np.array([len(x) for x in nl])
    if np.all(alllens == 0):
        return np.ones((dim[0], 1), dtype=dtype) * default

    if np.any([isinstance(x[0], list) for x in nl if len(x)]):
        maxz = 0
        for x in nl:
            if len(x):
                for y in x:
                    maxz = max(maxz, len(y))
        dim.append(maxz)
    arr = np.ones(dim, dtype=dtype) * default
    for i in range(dim[0]):
        if len(dim) == 2:
            arr[i, : len(nl[i])] = nl[i]
        elif len(dim) == 3:
            for j in range(len(nl[i])):
                arr[i, j, : len(nl[i][j])] = nl[i][j]
    return arr


def wildcard_substituted(type, params):
    """Substitutes wildcard atoms with the atoms in an atom tuple if possible

    Parameters
    ----------
    type : tuple
        atom type tuple
    params: collections.OrderedDict
        dictionary of atom types

    Returns
    -------
    prm : tuple
        Substituted or empty tuple
    """
    params = [atom_type for atom_type in params if 'X' in atom_type]
    for atom_types in params:
        match = True
        for i, atom_type in enumerate(atom_types):
            if atom_type != 'X':
                if atom_type != type[i]:
                    match = False
                    break
        if match:
            return atom_types
    return ()


def detectImproperCenter(indexes, graph):
    for i in indexes:
        if len(np.intersect1d(list(graph.neighbors(i)), indexes)) == 3:
            return i


def improperGraph(impropers, bonds):
    import networkx as nx

    g = nx.Graph()
    g.add_nodes_from(np.unique(impropers))
    g.add_edges_from([tuple(b) for b in bonds])
    return g


def getImproperParameter(type, parameters):
    from itertools import permutations

    type = np.array(type)
    perms = np.array([x for x in list(permutations((0, 1, 2, 3))) if x[2] == 2])
    for p in perms:
        if tuple(type[p]) in parameters.improper_types:
            return parameters.improper_types[tuple(type[p])], "improper_types"
        elif tuple(type[p]) in parameters.improper_periodic_types:
            return (
                parameters.improper_periodic_types[tuple(type[p])],
                "improper_periodic_types",
            )
        else:
            improper_params = wildcard_substituted(tuple(type[p]), parameters.improper_types)
            if improper_params:
                return parameters.improper_types[improper_params], "improper_types"
            improper_periodic_params = wildcard_substituted(tuple(type[p]), parameters.improper_periodic_types)
            if improper_periodic_params:
                return parameters.improper_periodic_types[improper_periodic_params], "improper_periodic_types"

    raise RuntimeError("Could not find improper parameters for key {}".format(type))


# TODO: Can be improved with lil sparse arrays
def init(mol, prm):
    natoms = mol.numAtoms
    charge = mol.charge.astype(np.float64)
    impropers = mol.impropers
    angles = mol.angles
    dihedrals = mol.dihedrals
    # if len(impropers) == 0:
    #     logger.warning('No impropers are defined in the input molecule. Check if this is correct. If not, use guessAnglesAndDihedrals.')
    # if len(angles) == 0:
    #     logger.warning('No angles are defined in the input molecule. Check if this is correct. If not, use guessAnglesAndDihedrals.')
    # if len(dihedrals) == 0:
    #     logger.warning('No dihedrals are defined in the input molecule. Check if this is correct. If not, use guessAnglesAndDihedrals.')

    if prm.urey_bradley_types:
        for type in prm.urey_bradley_types:
            if prm.urey_bradley_types[type].k != 0:
                logger.warning(
                    "Urey-Bradley types found in the parameters but are not implemented in FFEvaluate and will be ignored!"
                )
                break

    uqtypes, typeint = np.unique(mol.atomtype, return_inverse=True)
    sigma = np.zeros(len(uqtypes), dtype=np.float32)
    sigma14 = np.zeros(len(uqtypes), dtype=np.float32)
    epsilon = np.zeros(len(uqtypes), dtype=np.float32)
    epsilon14 = np.zeros(len(uqtypes), dtype=np.float32)
    for i, type in enumerate(uqtypes):
        sigma[i] = prm.atom_types[type].sigma
        epsilon[i] = prm.atom_types[type].epsilon
        sigma14[i] = prm.atom_types[type].sigma_14
        epsilon14[i] = prm.atom_types[type].epsilon_14

    nbfix = np.ones((len(prm.nbfix_types), 6), dtype=np.float64) * -1
    for i, nbf in enumerate(prm.nbfix_types):
        if nbf[0] in uqtypes and nbf[1] in uqtypes:
            idx1 = np.where(uqtypes == nbf[0])[0]
            idx2 = np.where(uqtypes == nbf[1])[0]
            rmin, eps, rmin14, eps14 = prm.atom_types[nbf[0]].nbfix[nbf[1]]
            sig = rmin * 2 ** (-1 / 6)  # Convert rmin to sigma
            sig14 = rmin14 * 2 ** (-1 / 6)
            nbfix[i, :] = [idx1, idx2, eps, sig, eps14, sig14]

    # 1-2 and 1-3 exclusion matrix
    # TODO: Don't read bonds / angles / dihedrals from mol. Read from forcefield
    excl_list = [[] for _ in range(natoms)]
    bond_pairs = [[] for _ in range(natoms)]
    bond_params = [[] for _ in range(natoms)]
    for bond in mol.bonds:
        types = tuple(uqtypes[typeint[bond]])
        bond = sorted(bond)
        excl_list[bond[0]].append(bond[1])
        bond_pairs[bond[0]].append(bond[1])
        bond_params[bond[0]].append(prm.bond_types[types].k)
        bond_params[bond[0]].append(prm.bond_types[types].req)
    angle_params = np.zeros((mol.angles.shape[0], 2), dtype=np.float32)
    for idx, angle in enumerate(mol.angles):
        first, second = sorted([angle[0], angle[2]])
        excl_list[first].append(second)
        types = tuple(uqtypes[typeint[angle]])
        angle_params[idx, :] = [
            prm.angle_types[types].k,
            radians(prm.angle_types[types].theteq),
        ]
    excl_list = [list(np.unique(x)) for x in excl_list]

    # 1-4 van der Waals scaling matrix
    s14_atom_list = [[] for _ in range(natoms)]
    s14_value_list = [[] for _ in range(natoms)]
    # 1-4 electrostatic scaling matrix
    e14_atom_list = [[] for _ in range(natoms)]
    e14_value_list = [[] for _ in range(natoms)]
    dihedral_params = [[] for _ in range(mol.dihedrals.shape[0])]
    alreadyadded = {}
    for idx, dihed in enumerate(mol.dihedrals):
        # Avoid readding duplicate dihedrals
        stringrep = " ".join(map(str, sorted(dihed)))
        if stringrep in alreadyadded:
            continue
        alreadyadded[stringrep] = True
        ty = tuple(uqtypes[typeint[dihed]])
        if ty in prm.dihedral_types:
            dihparam = prm.dihedral_types[ty]
        elif ty[::-1] in prm.dihedral_types:
            dihparam = prm.dihedral_types[ty[::-1]]
        else:
            dihparam = prm.dihedral_types[wildcard_substituted(ty, prm.dihedral_types)]
            if not dihparam:
                raise RuntimeError("Could not find type {} in dihedral_types".format(ty))
        i, j = sorted([dihed[0], dihed[3]])
        s14_atom_list[i].append(j)
        s14_value_list[i].append(dihparam[0].scnb)
        e14_atom_list[i].append(j)
        e14_value_list[i].append(dihparam[0].scee)
        for dip in dihparam:
            dihedral_params[idx].append(dip.phi_k)
            dihedral_params[idx].append(radians(dip.phase))
            dihedral_params[idx].append(dip.per)

    improper_params = np.zeros((mol.impropers.shape[0], 3), dtype=np.float32)
    graph = improperGraph(mol.impropers, mol.bonds)
    for idx, impr in enumerate(mol.impropers):
        ty = tuple(uqtypes[typeint[impr]])
        try:
            imprparam, impr_type = getImproperParameter(ty, prm)
        except Exception:
            try:  # In some cases AMBER does not store the center as 3rd atom (i.e. if it's index is 0). Then you need to detect it
                center = detectImproperCenter(impr, graph)
                notcenter = np.setdiff1d(impr, center)
                notcenter = sorted(uqtypes[typeint[notcenter]])
                ty = tuple(
                    notcenter[:2]
                    + [
                        uqtypes[typeint[center]],
                    ]
                    + notcenter[2:]
                )
                imprparam, impr_type = getImproperParameter(ty, prm)
            except Exception:
                raise RuntimeError(
                    "Could not find improper parameters for atom types {}".format(ty)
                )

        if impr_type == "improper_periodic_types":
            improper_params[idx, :] = [
                imprparam.phi_k,
                radians(imprparam.phase),
                imprparam.per,
            ]
        elif impr_type == "improper_types":
            improper_params[idx, :] = [imprparam.psi_k, radians(imprparam.psi_eq), 0]

    excl = nestedListToArray(excl_list, dtype=np.int64, default=-1)
    s14a = nestedListToArray(s14_atom_list, dtype=np.int64, default=-1)
    e14a = nestedListToArray(e14_atom_list, dtype=np.int64, default=-1)
    s14v = nestedListToArray(s14_value_list, dtype=np.float32, default=np.nan)
    e14v = nestedListToArray(e14_value_list, dtype=np.float32, default=np.nan)
    bonda = nestedListToArray(bond_pairs, dtype=np.int64, default=-1)
    bondv = nestedListToArray(bond_params, dtype=np.float32, default=np.nan)
    dihedral_params = nestedListToArray(
        dihedral_params, dtype=np.float32, default=np.nan
    )

    ELEC_FACTOR = 1 / (4 * const.pi * const.epsilon_0)  # Coulomb's constant
    ELEC_FACTOR *= (
        const.elementary_charge ** 2
    )  # Convert elementary charges to Coulombs
    ELEC_FACTOR /= const.angstrom  # Convert Angstroms to meters
    ELEC_FACTOR *= const.Avogadro / (
        const.kilo * const.calorie
    )  # Convert J to kcal/mol

    return (
        typeint,
        excl,
        nbfix,
        sigma,
        sigma14,
        epsilon,
        epsilon14,
        s14a,
        e14a,
        s14v,
        e14v,
        bonda,
        bondv,
        ELEC_FACTOR,
        charge,
        angles,
        angle_params,
        dihedrals,
        dihedral_params,
        impropers,
        improper_params,
    )


def calculateSets(mol, betweensets):
    setA = np.empty(0, dtype=int)
    setB = np.empty(0, dtype=int)
    if betweensets is not None:
        setA = mol.atomselect(betweensets[0], indexes=True)
        setB = mol.atomselect(betweensets[1], indexes=True)
        mol.bonds = np.empty((0, 2), dtype=np.uint32)
        mol.angles = np.empty((0, 3), dtype=np.uint32)
        mol.dihedrals = np.empty((0, 4), dtype=np.uint32)
        mol.impropers = np.empty((0, 4), dtype=np.uint32)
    return setA, setB


@jit("boolean(int64[:, :], int64, int64)", nopython=True)
def _ispaired(excl, i, j):
    nexcl = excl.shape[1]
    for e in range(nexcl):
        if excl[i, e] == -1:
            break
        if excl[i, e] == j:
            return True
    return False


@jit(nopython=True)
def _insets(i, j, set1, set2):
    nset1 = len(set1)
    nset2 = len(set2)
    if nset2 < nset1:
        tmp = set1
        set1 = set2
        set2 = tmp
        nset1 = len(set1)
        nset2 = len(set2)

    ifound = 0
    jfound = 0
    for k in range(nset1):
        if set1[k] == i and ifound == 0:
            ifound = 1
            break
        if set1[k] == j:
            jfound = 1
            break

    if ifound == 0 and jfound == 0:
        return False

    for k in range(nset2):
        if set2[k] == i:
            ifound = 2
            break
        if set2[k] == j:
            jfound = 2
            break

    if ifound != 0 and jfound != 0:
        if ifound != jfound:
            return True
    return False


@jit(nopython=True)
def _ffevaluate(
    coords,
    box,
    typeint,
    excl,
    nbfix,
    sigma,
    sigma14,
    epsilon,
    epsilon14,
    s14a,
    e14a,
    s14v,
    e14v,
    bonda,
    bondv,
    ELEC_FACTOR,
    charge,
    angles,
    angle_params,
    dihedrals,
    dihedral_params,
    impropers,
    improper_params,
    set1,
    set2,
    cutoff,
    rfa,
    solventDielectric,
):
    natoms = coords.shape[0]
    nframes = coords.shape[2]
    nangles = angles.shape[0]
    ndihedrals = dihedrals.shape[0]
    nimpropers = impropers.shape[0]
    direction_vec = np.zeros(3, dtype=np.float64)
    energies = np.zeros((6, nframes), dtype=np.float64)
    forces = np.zeros((natoms, 3, nframes), dtype=np.float64)
    atmnrg = np.zeros((natoms, 6, nframes), dtype=np.float64)
    usersets = (len(set1) > 0) | (len(set2) > 0)

    # Evaluate pair forces
    for f in range(nframes):
        for i in range(natoms):
            for j in range(i + 1, natoms):
                isbonded = _ispaired(bonda, i, j)
                isexcluded = _ispaired(excl, i, j)
                insets = _insets(i, j, set1, set2)

                if usersets and not insets:
                    continue

                if isexcluded and not isbonded:
                    continue

                dist = 0
                for k in range(3):
                    direction_vec[k] = wrapDistance(
                        coords[i, k, f] - coords[j, k, f], box[k, f]
                    )
                    dist += direction_vec[k] * direction_vec[k]
                dist = sqrt(dist)
                direction_unitvec = direction_vec / dist
                coeff = 0
                pot_bo = 0
                pot_lj = 0
                pot_el = 0
                if cutoff != 0 and dist > cutoff:
                    continue

                if isbonded:
                    pot_bo, force_bo = _evaluate_harmonic_bonds(
                        i, j, bonda, bondv, dist
                    )
                    energies[0, f] += pot_bo
                    coeff += force_bo
                if not isexcluded:
                    pot_lj, force_lj = _evaluate_lj(
                        i,
                        j,
                        typeint,
                        nbfix,
                        sigma,
                        sigma14,
                        epsilon,
                        epsilon14,
                        s14a,
                        s14v,
                        dist,
                    )
                    energies[1, f] += pot_lj
                    coeff += force_lj
                    pot_el, force_el = _evaluate_elec(
                        i,
                        j,
                        charge,
                        e14a,
                        e14v,
                        ELEC_FACTOR,
                        dist,
                        rfa,
                        solventDielectric,
                        cutoff,
                    )
                    energies[2, f] += pot_el
                    coeff += force_el

                atmnrg[i, 0, f] += pot_bo * 0.5
                atmnrg[j, 0, f] += pot_bo * 0.5
                atmnrg[i, 1, f] += pot_lj * 0.5
                atmnrg[j, 1, f] += pot_lj * 0.5
                atmnrg[i, 2, f] += pot_el * 0.5
                atmnrg[j, 2, f] += pot_el * 0.5
                for k in range(3):
                    forces[i, k, f] -= coeff * direction_unitvec[k]
                    forces[j, k, f] += coeff * direction_unitvec[k]

        if usersets:
            continue  # Don't calculate angles and dihedrals between sets of atoms

        # Evaluate angle forces
        for i in range(nangles):
            pot_an, force_an = _evaluate_angles(
                coords[angles[i, :], :, f], angle_params[i, :], box[:, f]
            )
            energies[3, f] += pot_an
            for a in range(3):
                for k in range(3):
                    forces[angles[i, a], k, f] += force_an[a, k]
                atmnrg[angles[i, a], 3, f] += pot_an / 3

        # Evaluate dihedral forces
        for i in range(ndihedrals):
            pot_di, force_di = _evaluate_torsion(
                coords[dihedrals[i, :], :, f], dihedral_params[i, :], box[:, f]
            )
            energies[4, f] += pot_di
            for d in range(4):
                for k in range(3):
                    forces[dihedrals[i, d], k, f] += force_di[d, k]
                atmnrg[dihedrals[i, d], 4, f] += pot_di / 4

        # Evaluate impropers
        for i in range(nimpropers):
            pot_im, force_im = _evaluate_torsion(
                coords[impropers[i, :], :, f], improper_params[i, :], box[:, f]
            )
            energies[5, f] += pot_im
            for d in range(4):
                for k in range(3):
                    forces[impropers[i, d], k, f] += force_im[d, k]
                atmnrg[impropers[i, d], 5, f] += pot_im / 4

    return energies, forces, atmnrg


@jit(
    "UniTuple(float64, 5)(int64, int64, int64, int64, float64[:,:], float32[:], float32[:], float32[:], float32[:], int64[:,:], float32[:,:])",
    nopython=True,
)
def _getSigmaEpsilon(
    i, j, it, jt, nbfix, sigma, sigma14, epsilon, epsilon14, s14a, s14v
):
    # i, j atom indexes  it, jt atom types
    n14 = s14a.shape[1]
    # Check if NBfix exists for the types and keep the index
    idx_nbfix = -1
    for k in range(nbfix.shape[0]):
        if (nbfix[k, 0] == it and nbfix[k, 1] == jt) or (
            nbfix[k, 0] == jt and nbfix[k, 1] == it
        ):
            idx_nbfix = k
            break

    scale = 1
    found14 = False
    for e in range(n14):
        if s14a[i, e] == -1:
            break
        if s14a[i, e] == j:
            found14 = True
            scale = s14v[i, e]
            break

    if idx_nbfix >= 0:
        eps = nbfix[idx_nbfix, 2]
        sig = nbfix[idx_nbfix, 3]
        if found14:
            eps = nbfix[idx_nbfix, 4]
            sig = nbfix[idx_nbfix, 5]
    else:
        sigmai = sigma[it]
        sigmaj = sigma[jt]
        epsiloni = epsilon[it]
        epsilonj = epsilon[jt]
        if found14:
            sigmai = sigma14[it]
            sigmaj = sigma14[jt]
            epsiloni = epsilon14[it]
            epsilonj = epsilon14[jt]
        # Lorentz - Berthelot combination rule
        sig = 0.5 * (sigmai + sigmaj)
        eps = sqrt(epsiloni * epsilonj)

    s2 = sig * sig
    s6 = s2 * s2 * s2
    s12 = s6 * s6
    A = eps * 4 * s12
    B = eps * 4 * s6
    return sig, eps, A, B, scale


@jit(
    "UniTuple(float64, 2)(int64, int64, int64[:], float64[:,:], float32[:], float32[:], float32[:], float32[:], int64[:,:], float32[:,:], float64)",
    nopython=True,
)
def _evaluate_lj(
    i, j, typeint, nbfix, sigma, sigma14, epsilon, epsilon14, s14a, s14v, dist
):
    _, _, A, B, scale = _getSigmaEpsilon(
        i,
        j,
        typeint[i],
        typeint[j],
        nbfix,
        sigma,
        sigma14,
        epsilon,
        epsilon14,
        s14a,
        s14v,
    )

    # cutoff = 2.5 * sig
    # if dist < cutoff:
    rinv1 = 1 / dist
    rinv2 = rinv1 * rinv1
    rinv6 = rinv2 * rinv2 * rinv2
    rinv12 = rinv6 * rinv6
    pot = ((A * rinv12) - (B * rinv6)) / scale
    force = (-12 * A * rinv12 + 6 * B * rinv6) * rinv1 / scale
    return pot, force


@jit(
    "UniTuple(float64, 2)(int64, int64, int64[:,:], float32[:,:], float64)",
    nopython=True,
)
def _evaluate_harmonic_bonds(i, j, bonda, bondv, dist):
    nbonds = bonda.shape[1]
    bonded = False
    col = -1
    for e in range(nbonds):
        if bonda[i, e] == -1:
            break
        if bonda[i, e] == j:
            bonded = True
            col = e
            break
    if not bonded:
        return 0, 0

    k0 = bondv[i, col * 2 + 0]
    d0 = bondv[i, col * 2 + 1]
    x = dist - d0
    pot = k0 * (x ** 2)
    force = 2 * k0 * x
    return pot, force


@jit(
    "UniTuple(float64, 2)(int64, int64, float64[:], int64[:,:], float32[:,:], float64, float64, boolean, float64, float64)",
    nopython=True,
)
def _evaluate_elec(
    i, j, charge, e14a, e14v, ELEC_FACTOR, dist, rfa, solventDielectric, cutoff
):
    nelec = e14a.shape[1]
    scale = 1
    for e in range(nelec):
        if e14a[i, e] == -1:
            break
        if e14a[i, e] == j:
            scale = e14v[i, e]
            break

    if rfa:  # Reaction field approximation for electrostatics with cutoff
        # http://docs.openmm.org/latest/userguide/theory.html#coulomb-interaction-with-cutoff
        # Ilario G. Tironi, René Sperb, Paul E. Smith, and Wilfred F. van Gunsteren. A generalized reaction field method
        # for molecular dynamics simulations. Journal of Chemical Physics, 102(13):5451–5459, 1995.
        denom = (2 * solventDielectric) + 1
        krf = (1 / cutoff ** 3) * (solventDielectric - 1) / denom
        crf = (1 / cutoff) * (3 * solventDielectric) / denom
        common = ELEC_FACTOR * charge[i] * charge[j] / scale
        dist2 = dist ** 2
        pot = common * ((1 / dist) + krf * dist2 - crf)
        force = common * (2 * krf * dist - 1 / dist2)
    else:
        pot = ELEC_FACTOR * charge[i] * charge[j] / dist / scale
        force = -pot / dist
    return pot, force


@jit(nopython=True)
def _evaluate_angles(pos, angle_params, box):
    k0 = angle_params[0]
    theta0 = angle_params[1]

    force = np.zeros((3, 3), dtype=np.float64)
    r23 = np.zeros(3)
    r21 = np.zeros(3)
    norm23 = 0
    norm21 = 0
    dotprod = 0
    for i in range(3):
        r23[i] = wrapBondedDistance(pos[2, i] - pos[1, i], box[i])
        r21[i] = wrapBondedDistance(pos[0, i] - pos[1, i], box[i])
        dotprod += r23[i] * r21[i]
        norm23 += r23[i] * r23[i]
        norm21 += r21[i] * r21[i]
    norm23inv = 1 / sqrt(norm23)
    norm21inv = 1 / sqrt(norm21)

    cos_theta = dotprod * norm21inv * norm23inv
    if cos_theta < -1.0:
        cos_theta = -1.0
    if cos_theta > 1.0:
        cos_theta = 1.0
    theta = acos(cos_theta)

    delta_theta = theta - theta0
    pot = k0 * delta_theta * delta_theta

    # # OpenMM version - There is a bug in the signs somewhere
    # dEdTheta = 2 * k0 * delta_theta
    # thetaCross = cross(r21, r23)
    # lengthThetaCross = sqrt(dot(thetaCross, thetaCross))
    # termA = dEdTheta * np.sign(r23) / (norm21 * lengthThetaCross)
    # termC = -dEdTheta * np.sign(r21) / (norm23 * lengthThetaCross)
    # deltaCross1 = cross(r21, thetaCross)
    # deltaCross2 = cross(r23, thetaCross)
    # force[0, :] = termA * deltaCross1
    # force[2, :] = termC * deltaCross2
    # force[1, :] = -(force[0, :]+force[2, :])
    # print(force[0, 0], force[0, 1], force[0, 2])
    # print(force[1, 0], force[1, 1], force[1, 2])
    # print(force[2, 0], force[2, 1], force[2, 2])

    sin_theta = sqrt(1.0 - cos_theta * cos_theta)
    coef = 0
    if sin_theta != 0:
        coef = -2.0 * k0 * delta_theta / sin_theta

    for i in range(3):
        force[0, i] = (
            coef * (cos_theta * r21[i] * norm21inv - r23[i] * norm23inv) * norm21inv
        )
        force[2, i] = (
            coef * (cos_theta * r23[i] * norm23inv - r21[i] * norm21inv) * norm23inv
        )
        force[1, i] = -(force[0, i] + force[2, i])

    # TODO: Return the actual force. Problem with numba UniTuple
    return pot, force


@jit(nopython=True)
def _evaluate_torsion(pos, torsionparam, box):  # Dihedrals and impropers
    ntorsions = int(len(torsionparam) / 3)
    for i in range(len(torsionparam)):
        if np.isnan(torsionparam[i]):
            ntorsions = int(i / 3)
            break
    pot = 0
    force = np.zeros((4, 3), dtype=np.float64)
    phi, r12, r23, r34 = dihedralAngleFull(pos, box)
    coef = 0

    for i in range(0, ntorsions):
        k0 = torsionparam[i * 3 + 0]
        phi0 = torsionparam[i * 3 + 1]
        per = torsionparam[i * 3 + 2]  # Periodicity

        if per > 0:  # Proper dihedrals or periodic improper dihedrals
            pot += k0 * (1 + cos(per * phi - phi0))
            coef += -per * k0 * sin(per * phi - phi0)
        else:  # Non-periodic improper dihedrals
            diff = phi - phi0
            if diff < -pi:
                diff += 2 * pi
            elif diff > pi:
                diff -= 2 * pi
            pot += k0 * diff ** 2
            coef += 2 * k0 * diff

    # Taken from OpenMM
    dEdTheta = coef
    cross1 = cross(r12, r23)
    cross2 = cross(r23, r34)
    norm2Delta2 = dot(r23, r23)
    normDelta2 = sqrt(norm2Delta2)
    normCross1 = dot(cross1, cross1)
    normCross2 = dot(cross2, cross2)
    normBC = normDelta2
    forceFactors = np.zeros(4)
    forceFactors[0] = (-dEdTheta * normBC) / normCross1
    forceFactors[3] = (dEdTheta * normBC) / normCross2
    forceFactors[1] = dot(r12, r23)
    forceFactors[1] /= norm2Delta2
    forceFactors[2] = dot(r34, r23)
    forceFactors[2] /= norm2Delta2
    force1 = forceFactors[0] * cross1
    force4 = forceFactors[3] * cross2
    s = forceFactors[1] * force1 - forceFactors[2] * force4
    force[0, :] -= force1
    force[1, :] += force1 + s
    force[2, :] += force4 - s
    force[3, :] -= force4

    return pot, force


def _drawForce(start, vec):
    assert start.ndim == 1 and vec.ndim == 1
    from moleculekit.vmdviewer import getCurrentViewer

    vmd = getCurrentViewer()
    vmd.send(
        """
    proc vmd_draw_arrow {start end} {
        # an arrow is made of a cylinder and a cone
        draw color green
        set middle [vecadd $start [vecscale 0.9 [vecsub $end $start]]]
        graphics top cylinder $start $middle radius 0.15
        graphics top cone $middle $end radius 0.25
    }
    """
    )
    vmd.send(
        "vmd_draw_arrow {{ {} }} {{ {} }}".format(
            " ".join(map(str, start)), " ".join(map(str, start + vec))
        )
    )


def viewForces(mol, forces, frame=0):
    """Visualize force vectors in VMD

    Parameters
    ----------
    mol : Molecule
        The Molecule with the coordinates on which to visualize the forces
    forces : np.ndarray
        The force array produced by FFEvaluate
    frame : int
        The coordinate frame for which to show the forces and coordinates
    """
    mol.view()
    for cc, ff in zip(mol.coords[:, :, frame], forces[:, :, frame]):
        _drawForce(cc, ff)
