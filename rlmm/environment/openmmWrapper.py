import copy
import math
from io import StringIO

import numpy as np
import simtk.openmm as mm
from openmmtools import cache
from openmmtools import integrators
from openmmtools.mcmc import HMCMove, WeightedMove, MCMCSampler, LangevinSplittingDynamicsMove, SequenceMove, \
    MCDisplacementMove, MCRotationMove, GHMCMove, LangevinDynamicsMove
from openmmtools import multistate
from openmmtools.states import ThermodynamicState, SamplerState
from simtk import unit
from simtk.openmm import app

from rlmm.utils.config import Config
from rlmm.utils.loggers import make_message_writer


class SystemParams(Config):
    def __init__(self, config_dict):
        self.platform = None
        self.minMaxIters = None
        self.integrator_setConstraintTolerance = None
        self.platform_config = None
        self.integrator_params = None
        for k, v in config_dict.items():
            if k != "platform_config" and isinstance(v, dict):
                for k_, v_ in v.items():
                    try:
                        exec('v[k_] = ' + v_)
                    except TypeError:
                        v[k_] = v_
            else:
                exec('config_dict[k] = ' + str(v))
        self.__dict__.update(config_dict)

class MCMCReplicaOpenMMSimulationWrapper:
    class Config(Config):
        def __init__(self, args):
            self.hybrid = None
            self.ligand_pertubation_samples = None
            self.displacement_sigma = None
            self.verbose = None
            self.n_steps = None
            self.n_replicas = 3
            self.T_min = 298.0 * unit.kelvin
            self.T_max = 600.0 * unit.kelvin
            self.parameters = SystemParams(args['params'])
            self.systemloader = None
            if args is not None:
                self.__dict__.update(args)

        def get_obj(self, system_loader, *args, **kwargs):
            self.systemloader = system_loader
            return MCMCReplicaOpenMMSimulationWrapper(self, *args, **kwargs)

    def __init__(self, config_: Config, old_sampler_state=None):
        """

        :param systemLoader:
        :param config:
        """
        self.config = config_
        self.logger = make_message_writer(self.config.verbose, self.__class__.__name__)
        with self.logger("__init__") as logger:

            if self.config.systemloader.system is None:
                system = self.config.systemloader.get_system(self.config.parameters.createSystem)
                cache.global_context_cache.set_platform(self.config.parameters.platform,
                                                        self.config.parameters.platform_config)
                cache.global_context_cache.time_to_live = 10
                prot_atoms = None
            else:
                system = self.config.systemloader.system

                past_sampler_state_velocities = old_sampler_state.sampler.sampler_state.velocities
                prot_atoms = list(self.config.systemloader.get_selection_protein())
            self.system = system
            self.topology = self.config.systemloader.get_topology()


            temperatures = [self.config.T_min + (self.config.T_max - self.config.T_min) * (math.exp(float(i) / float(self.config.n_replicas - 1)) - 1.0) / (math.e - 1.0) for i in range(self.config.n_replicas)]
            self.thermodynamic_states = [ThermodynamicState(system=system, temperature=T) for T in temperatures]

            atoms = list(set(self.config.systemloader.get_selection_ligand()))
            subset_move = MCDisplacementMove(atom_subset=atoms,
                                             displacement_sigma=self.config.displacement_sigma * unit.angstrom)
            subset_rot = MCRotationMove(atom_subset=atoms)
            ghmc_move = GHMCMove(timestep=self.config.parameters.integrator_params['timestep'],
                                 n_steps=self.config.n_steps,
                                 collision_rate=self.config.parameters.integrator_params['collision_rate'])

            langevin_move = LangevinSplittingDynamicsMove(
                timestep=self.config.parameters.integrator_params['timestep'],
                n_steps=self.config.n_steps,
                collision_rate=self.config.parameters.integrator_params['collision_rate'],
                reassign_velocities=True,
                n_restart_attempts=6,
                constraint_tolerance=self.config.parameters.integrator_setConstraintTolerance)

            if self.config.hybrid:
                langevin_move_weighted = WeightedMove([ (ghmc_move, 0.5),
                                              (langevin_move, 0.5)])
                sequence_move = SequenceMove([subset_move, subset_rot, langevin_move_weighted])
            else:
                sequence_move = SequenceMove([ langevin_move])

            self.simulation = multistate.MultiStateSampler(mcmc_moves=sequence_move, number_of_iterations=np.inf)
            storage_path = self.config.tempdir +  'multistate.nc'
            self.reporter = multistate.MultiStateReporter(storage_path, checkpoint_interval=1)
            self.simulation.create(thermodynamic_states=self.thermodynamic_states,sampler_states = [SamplerState(self.config.systemloader.get_positions()) for i in range(self.config.n_replicas)], storage = self.reporter)

            self.simulation.minimize(max_iterations=self.config.parameters.minMaxIters)
            # print(self.sampler.sampler_state.box_vectors)

            # exit()
            # if prot_atoms is not None:
            #     new_vels = self.sampler.sampler_state.velocities
            #     for i in prot_atoms:
            #         new_vels[i] = past_sampler_state_velocities[i]
            #     self.sampler.sampler_state._set_velocities(new_vels, False)
            #     self.sampler_state._set_velocities(new_vels, False)

    def run(self, steps):
        """

        :param steps:
        """
        # for j in range(self.config.ligand_pertubation_samples - 1):
        #     self.sampler.move.move_list[0].apply(self.sampler.thermodynamic_state, self.sampler.sampler_state)
        #     self.sampler.move.move_list[1].apply(self.sampler.thermodynamic_state, self.sampler.sampler_state)
        self.simulation.run(steps)

    def get_sim_time(self):
        return self.config.n_steps * self.config.parameters.integrator_params['timestep'] * self.config.n_replicas

    def get_coordinates(self):
        """

        :return:
        """
        return self.simulation.sampler_states[1].positions

    def get_pdb(self, file_name=None):
        """

        :return:
        """
        if file_name is None:
            output = StringIO()
        else:
            output = open(file_name, 'w')

        app.PDBFile.writeFile(self.topology,
                              self.get_coordinates(),
                              file=output)
        if file_name is None:
            return output.getvalue()
        else:
            output.close()
            return True

    def get_enthalpies(self, groups=None):
        #TODO
        return 0


class MCMCOpenMMSimulationWrapper:
    class Config(Config):
        def __init__(self, args):
            self.hybrid = None
            self.ligand_pertubation_samples = None
            self.displacement_sigma = None
            self.verbose = None
            self.n_steps = None
            self.parameters = SystemParams(args['params'])
            self.systemloader = None
            if args is not None:
                self.__dict__.update(args)

        def get_obj(self, system_loader, *args, **kwargs):
            self.systemloader = system_loader
            return MCMCOpenMMSimulationWrapper(self, *args, **kwargs)

    def rearrange_forces_implicit(self, system):
        protein_index = set(self.config.systemloader.get_selection_protein())
        ligand_index = set(self.config.systemloader.get_selection_ligand())
        try:
            assert (len(protein_index.union(ligand_index)) == system.getNumParticles())
        except AssertionError:
            print('len prot', len(protein_index), 'len_ligand', len(ligand_index), 'union',
                  len(protein_index.union(ligand_index)), system.getNumParticles(), min(ligand_index),
                  max(ligand_index), min(protein_index), max(protein_index))
            exit()
        nb_id = None
        fb_id = None
        for force_idnum, force in enumerate(system.getForces()):
            if force.__class__.__name__ in ['CMMotionRemover']:  # valence
                force.setForceGroup(0)
            elif force.__class__.__name__ in ['NonbondedForce']:
                force.setForceGroup(1)
                nb_id = force_idnum
            elif force.__class__.__name__ in ['CustomGBForce']:
                force.setForceGroup(1)
                fb_id = force_idnum

        system.addForce(copy.deepcopy(system.getForce(nb_id)))
        new_id = len(system.getForces()) - 1
        for ligand_atom in ligand_index:
            _, sigma, _ = system.getForce(new_id).getParticleParameters(ligand_atom)
            system.getForce(new_id).setParticleParameters(ligand_atom, 0, sigma, 0)
        for i in range(system.getForce(new_id).getNumExceptions()):
            data = system.getForce(new_id).getExceptionParameters(i)
            if data[0] in ligand_index or data[1] in ligand_index:
                system.getForce(new_id).setExceptionParameters(i, data[0], data[1], 0, data[3], 0)
        system.getForce(new_id).setForceGroup(2)

        system.addForce(copy.deepcopy(system.getForce(fb_id)))
        new_id = len(system.getForces()) - 1
        for ts, ligand_atom in enumerate(ligand_index):
            idata = system.getForce(new_id).getParticleParameters(ligand_atom)
            dl2 = list(idata)
            dl2[0] = 0.0
            dl2 = tuple(dl2)
            system.getForce(new_id).setParticleParameters(ligand_atom, dl2)
        system.getForce(new_id).setForceGroup(2)

        system.addForce(copy.deepcopy(system.getForce(nb_id)))
        new_id = len(system.getForces()) - 1
        for protein_atom in protein_index:
            system.getForce(new_id).setParticleParameters(protein_atom, 0, 0, 0)
        for i in range(system.getForce(new_id).getNumExceptions()):
            data = system.getForce(new_id).getExceptionParameters(i)
            if data[0] in ligand_index and data[1] in protein_index:
                system.getForce(new_id).setExceptionParameters(i, data[0], data[1], 0, 0, 0)
            elif data[0] in protein_index and data[1] in ligand_index:
                system.getForce(new_id).setExceptionParameters(i, data[0], data[1], 0, 0, 0)
            elif data[0] in protein_index and data[1] in protein_index:
                system.getForce(new_id).setExceptionParameters(i, data[0], data[1], 0, 0, 0)
        system.getForce(new_id).setForceGroup(3)

        system.addForce(copy.deepcopy(system.getForce(fb_id)))
        new_id = len(system.getForces()) - 1
        for ts, protein_atom in enumerate(protein_index):
            idata = system.getForce(new_id).getParticleParameters(protein_atom)
            dl2 = list(idata)
            dl2[0] = 0.0
            dl2 = tuple(dl2)
            system.getForce(new_id).setParticleParameters(protein_atom, dl2)
        system.getForce(new_id).setForceGroup(3)

        bad_ids = []

        harmonic_bond_force_com = mm.HarmonicBondForce()
        harmonic_bond_force_apo = mm.HarmonicBondForce()
        harmonic_bond_force_lig = mm.HarmonicBondForce()

        periodic_torsion_force_com = mm.PeriodicTorsionForce()
        periodic_torsion_force_apo = mm.PeriodicTorsionForce()
        periodic_torsion_force_lig = mm.PeriodicTorsionForce()

        harmonic_angle_force_com = mm.HarmonicAngleForce()
        harmonic_angle_force_apo = mm.HarmonicAngleForce()
        harmonic_angle_force_lig = mm.HarmonicAngleForce()

        protein_index = set(self.config.systemloader.get_selection_protein())
        ligand_index = set(self.config.systemloader.get_selection_ligand())

        for id_name, force in enumerate(system.getForces()):
            if force.__class__.__name__ in ['HarmonicAngleForce']:
                bad_ids.append(id_name)
                for i in range(force.getNumAngles()):
                    args = force.getAngleParameters(i)
                    harmonic_angle_force_com.addAngle(*args)
                    if all(map(lambda pos_: pos_ in protein_index, args[:3])):
                        harmonic_angle_force_apo.addAngle(*args)
                    elif all(map(lambda pos_: pos_ in ligand_index, args[:3])):
                        harmonic_angle_force_lig.addAngle(*args)
                    else:
                        assert (False)

            elif force.__class__.__name__ in ['HarmonicBondForce']:
                bad_ids.append(id_name)
                for i in range(force.getNumBonds()):
                    args = force.getBondParameters(i)
                    harmonic_bond_force_com.addBond(*args)
                    if all(map(lambda pos_: pos_ in protein_index, args[:2])):
                        harmonic_bond_force_apo.addBond(*args)
                    elif all(map(lambda pos_: pos_ in ligand_index, args[:2])):
                        harmonic_bond_force_lig.addBond(*args)
                    else:
                        assert (False)

            elif force.__class__.__name__ in ['PeriodicTorsionForce']:
                bad_ids.append(id_name)
                for i in range(force.getNumTorsions()):
                    args = force.getTorsionParameters(i)
                    periodic_torsion_force_com.addTorsion(*args)
                    if all(map(lambda pos_: pos_ in protein_index, args[:4])):
                        periodic_torsion_force_apo.addTorsion(*args)
                    elif all(map(lambda pos_: pos_ in ligand_index, args[:4])):
                        periodic_torsion_force_lig.addTorsion(*args)
                    else:
                        assert (False)

        bad_ids.sort(reverse=True)
        for bad_id in bad_ids:
            system.removeForce(bad_id)

        fcount = len(system.getForces())
        system.addForce(harmonic_angle_force_com)
        system.getForce(fcount).setForceGroup(1)
        fcount += 1
        system.addForce(harmonic_angle_force_apo)
        system.getForce(fcount).setForceGroup(2)
        fcount += 1
        system.addForce(harmonic_angle_force_lig)
        system.getForce(fcount).setForceGroup(3)
        fcount += 1

        system.addForce(periodic_torsion_force_com)
        system.getForce(fcount).setForceGroup(1)
        fcount += 1
        system.addForce(periodic_torsion_force_apo)
        system.getForce(fcount).setForceGroup(2)
        fcount += 1
        system.addForce(periodic_torsion_force_lig)
        system.getForce(fcount).setForceGroup(3)
        fcount += 1

        system.addForce(harmonic_bond_force_com)
        system.getForce(fcount).setForceGroup(1)
        fcount += 1
        system.addForce(harmonic_bond_force_apo)
        system.getForce(fcount).setForceGroup(2)
        fcount += 1
        system.addForce(harmonic_bond_force_lig)
        system.getForce(fcount).setForceGroup(3)
        fcount += 1

        system.addForce(mm.RMSDForce(self.config.systemloader.get_positions(), list(protein_index)))
        system.getForce(fcount).setForceGroup(4)

    def __init__(self, config_: Config, old_sampler_state=None):
        """

        :param systemLoader:
        :param config:
        """
        self.config = config_
        self.logger = make_message_writer(self.config.verbose, self.__class__.__name__)
        with self.logger("__init__") as logger:

            if self.config.systemloader.system is None:
                system = self.config.systemloader.get_system(self.config.parameters.createSystem)
                cache.global_context_cache.set_platform(self.config.parameters.platform,
                                                        self.config.parameters.platform_config)
                cache.global_context_cache.time_to_live = 10
                prot_atoms = None
            else:
                system = self.config.systemloader.system
                past_sampler_state_velocities = old_sampler_state.sampler.sampler_state.velocities
                prot_atoms = list(self.config.systemloader.get_selection_protein())

            self.topology = self.config.systemloader.get_topology()

            self.thermodynamic_state = ThermodynamicState(system=system,
                                                          temperature=self.config.parameters.integrator_params[
                                                              'temperature'])

            self.sampler_state = SamplerState(positions=self.config.systemloader.get_positions(), box_vectors=self.config.systemloader.boxvec)

            print(self.sampler_state.box_vectors)
            atoms = list(set(self.config.systemloader.get_selection_ligand()))
            subset_move = MCDisplacementMove(atom_subset=atoms,
                                             displacement_sigma=self.config.displacement_sigma * unit.angstrom)
            subset_rot = MCRotationMove(atom_subset=atoms)
            ghmc_move = GHMCMove(timestep=self.config.parameters.integrator_params['timestep'],
                                 n_steps=self.config.n_steps,
                                 collision_rate=self.config.parameters.integrator_params['collision_rate'])

            langevin_move = LangevinDynamicsMove(
                timestep=self.config.parameters.integrator_params['timestep'],
                n_steps=self.config.n_steps,
                collision_rate=self.config.parameters.integrator_params['collision_rate'],
                reassign_velocities=True,
                n_restart_attempts=6)
                # constraint_tolerance=self.config.parameters.integrator_setConstraintTolerance)

            if self.config.hybrid:
                langevin_move_weighted = WeightedMove([ (ghmc_move, 0.5),
                                              (langevin_move, 0.5)])
                sequence_move = SequenceMove([subset_move, subset_rot, langevin_move_weighted])
            else:
                sequence_move = SequenceMove([ langevin_move])

            self.sampler = MCMCSampler(self.thermodynamic_state, self.sampler_state, move=sequence_move)
            self.get_pdb('test1af.pdb')
            self.sampler.minimize(max_iterations=self.config.parameters.minMaxIters)
            # print(self.sampler.sampler_state.box_vectors)
            self.get_pdb('test2af.pdb')

            # exit()
            if prot_atoms is not None:
                new_vels = self.sampler.sampler_state.velocities
                for i in prot_atoms:
                    new_vels[i] = past_sampler_state_velocities[i]
                self.sampler.sampler_state._set_velocities(new_vels, False)
                self.sampler_state._set_velocities(new_vels, False)

    def run(self, steps):
        """

        :param steps:
        """
        # for j in range(self.config.ligand_pertubation_samples - 1):
        #     self.sampler.move.move_list[0].apply(self.sampler.thermodynamic_state, self.sampler.sampler_state)
        #     self.sampler.move.move_list[1].apply(self.sampler.thermodynamic_state, self.sampler.sampler_state)
        self.sampler.run(steps)

    def get_sim_time(self):
        return self.config.n_steps * self.config.parameters.integrator_params['timestep']

    def get_coordinates(self):
        """

        :return:
        """
        return self.sampler.sampler_state.positions

    def get_pdb(self, file_name=None):
        """

        :return:
        """
        if file_name is None:
            output = StringIO()
        else:
            output = open(file_name, 'w')

        app.PDBFile.writeFile(self.topology,
                              self.sampler.sampler_state.positions,
                              file=output)
        if file_name is None:
            return output.getvalue()
        else:
            output.close()
            return True

    def get_enthalpies(self, groups=None):
        return cache.global_context_cache.get_context(self.thermodynamic_state)[0].getState(getEnergy=True,
                                                                                            groups=groups).getPotentialEnergy().value_in_unit(
            unit.kilojoule / unit.mole)


class OpenMMSimulationWrapper:
    class Config(Config):
        def __init__(self, args):
            self.parameters = SystemParams(args['params'])
            self.systemloader = None
            if args is not None:
                self.__dict__.update(args)

        def get_obj(self, system_loader, *args, **kwargs):
            self.systemloader = system_loader
            return OpenMMSimulationWrapper(self, *args, **kwargs)

    def rearrange_forces_implicit(self, system):
        protein_index = set(self.config.systemloader.get_selection_protein())
        ligand_index = set(self.config.systemloader.get_selection_ligand())
        assert (len(protein_index.union(ligand_index)) == system.getNumParticles())
        nb_id = None
        fb_id = None
        for force_idnum, force in enumerate(system.getForces()):
            if force.__class__.__name__ in ['CMMotionRemover']:  # valence
                force.setForceGroup(0)
            elif force.__class__.__name__ in ['NonbondedForce']:
                force.setForceGroup(1)
                nb_id = force_idnum
            elif force.__class__.__name__ in ['CustomGBForce']:
                force.setForceGroup(1)
                fb_id = force_idnum

        system.addForce(copy.deepcopy(system.getForce(nb_id)))
        new_id = len(system.getForces()) - 1
        for ligand_atom in ligand_index:
            system.getForce(new_id).setParticleParameters(ligand_atom, 0, 0, 0)
        for i in range(system.getForce(new_id).getNumExceptions()):
            data = system.getForce(new_id).getExceptionParameters(i)
            if data[0] in ligand_index and data[1] in protein_index:
                system.getForce(new_id).setExceptionParameters(i, data[0], data[1], 0, 0, 0)
            elif data[0] in protein_index and data[1] in ligand_index:
                system.getForce(new_id).setExceptionParameters(i, data[0], data[1], 0, 0, 0)
            elif data[0] in ligand_index and data[1] in ligand_index:
                system.getForce(new_id).setExceptionParameters(i, data[0], data[1], 0, 0, 0)
        system.getForce(new_id).setForceGroup(2)

        system.addForce(copy.deepcopy(system.getForce(fb_id)))
        new_id = len(system.getForces()) - 1
        for ts, ligand_atom in enumerate(ligand_index):
            idata = system.getForce(new_id).getParticleParameters(ligand_atom)
            dl2 = list(idata)
            dl2[0] = 0.0
            dl2 = tuple(dl2)
            system.getForce(new_id).setParticleParameters(ligand_atom, dl2)
        system.getForce(new_id).setForceGroup(2)

        system.addForce(copy.deepcopy(system.getForce(nb_id)))
        new_id = len(system.getForces()) - 1
        for protein_atom in protein_index:
            system.getForce(new_id).setParticleParameters(protein_atom, 0, 0, 0)
        for i in range(system.getForce(new_id).getNumExceptions()):
            data = system.getForce(new_id).getExceptionParameters(i)
            if data[0] in ligand_index and data[1] in protein_index:
                system.getForce(new_id).setExceptionParameters(i, data[0], data[1], 0, 0, 0)
            elif data[0] in protein_index and data[1] in ligand_index:
                system.getForce(new_id).setExceptionParameters(i, data[0], data[1], 0, 0, 0)
            elif data[0] in protein_index and data[1] in protein_index:
                system.getForce(new_id).setExceptionParameters(i, data[0], data[1], 0, 0, 0)
        system.getForce(new_id).setForceGroup(3)

        system.addForce(copy.deepcopy(system.getForce(fb_id)))
        new_id = len(system.getForces()) - 1
        for ts, protein_atom in enumerate(protein_index):
            idata = system.getForce(new_id).getParticleParameters(protein_atom)
            dl2 = list(idata)
            dl2[0] = 0.0
            dl2 = tuple(dl2)
            system.getForce(new_id).setParticleParameters(protein_atom, dl2)
        system.getForce(new_id).setForceGroup(3)

        bad_ids = []

        harmonic_bond_force_com = mm.HarmonicBondForce()
        harmonic_bond_force_apo = mm.HarmonicBondForce()
        harmonic_bond_force_lig = mm.HarmonicBondForce()

        periodic_torsion_force_com = mm.PeriodicTorsionForce()
        periodic_torsion_force_apo = mm.PeriodicTorsionForce()
        periodic_torsion_force_lig = mm.PeriodicTorsionForce()

        harmonic_angle_force_com = mm.HarmonicAngleForce()
        harmonic_angle_force_apo = mm.HarmonicAngleForce()
        harmonic_angle_force_lig = mm.HarmonicAngleForce()

        protein_index = set(self.config.systemloader.get_selection_protein())
        ligand_index = set(self.config.systemloader.get_selection_ligand())

        for id_name, force in enumerate(system.getForces()):
            if force.__class__.__name__ in ['HarmonicAngleForce']:
                bad_ids.append(id_name)
                for i in range(force.getNumAngles()):
                    args = force.getAngleParameters(i)
                    harmonic_angle_force_com.addAngle(*args)
                    if all(map(lambda pos_: pos_ in protein_index, args[:3])):
                        harmonic_angle_force_apo.addAngle(*args)
                    elif all(map(lambda pos_: pos_ in ligand_index, args[:3])):
                        harmonic_angle_force_lig.addAngle(*args)
                    else:
                        assert (False)

            elif force.__class__.__name__ in ['HarmonicBondForce']:
                bad_ids.append(id_name)
                for i in range(force.getNumBonds()):
                    args = force.getBondParameters(i)
                    harmonic_bond_force_com.addBond(*args)
                    if all(map(lambda pos_: pos_ in protein_index, args[:2])):
                        harmonic_bond_force_apo.addBond(*args)
                    elif all(map(lambda pos_: pos_ in ligand_index, args[:2])):
                        harmonic_bond_force_lig.addBond(*args)
                    else:
                        assert (False)

            elif force.__class__.__name__ in ['PeriodicTorsionForce']:
                bad_ids.append(id_name)
                for i in range(force.getNumTorsions()):
                    args = force.getTorsionParameters(i)
                    periodic_torsion_force_com.addTorsion(*args)
                    if all(map(lambda pos_: pos_ in protein_index, args[:4])):
                        periodic_torsion_force_apo.addTorsion(*args)
                    elif all(map(lambda pos_: pos_ in ligand_index, args[:4])):
                        periodic_torsion_force_lig.addTorsion(*args)
                    else:
                        assert (False)

        bad_ids.sort(reverse=True)
        for bad_id in bad_ids:
            system.removeForce(bad_id)

        fcount = len(system.getForces())
        system.addForce(harmonic_angle_force_com)
        system.getForce(fcount).setForceGroup(1)
        fcount += 1
        system.addForce(harmonic_angle_force_apo)
        system.getForce(fcount).setForceGroup(2)
        fcount += 1
        system.addForce(harmonic_angle_force_lig)
        system.getForce(fcount).setForceGroup(3)
        fcount += 1

        system.addForce(periodic_torsion_force_com)
        system.getForce(fcount).setForceGroup(1)
        fcount += 1
        system.addForce(periodic_torsion_force_apo)
        system.getForce(fcount).setForceGroup(2)
        fcount += 1
        system.addForce(periodic_torsion_force_lig)
        system.getForce(fcount).setForceGroup(3)
        fcount += 1

        system.addForce(harmonic_bond_force_com)
        system.getForce(fcount).setForceGroup(1)
        fcount += 1
        system.addForce(harmonic_bond_force_apo)
        system.getForce(fcount).setForceGroup(2)
        fcount += 1
        system.addForce(harmonic_bond_force_lig)
        system.getForce(fcount).setForceGroup(3)
        fcount += 1

        system.addForce(mm.RMSDForce(self.config.systemloader.get_positions(), list(protein_index)))
        system.getForce(fcount).setForceGroup(4)

    def __init__(self, config_: Config, ln=None, prior_sim=None):
        """

        :param systemLoader:
        :param config:
        """
        self.config = config_
        if ln is None:
            system = self.config.systemloader.get_system(self.config.parameters.createSystem)
        else:
            system = self.config.systemloader.system

        # self.rearrange_forces_implicit(system)
        # integrator = integrators.LangevinIntegrator(splitting='V0 V1 R O R V1 V0',
        #                                             temperature=self.config.parameters.integrator_params['temperature'],
        #                                             timestep=self.config.parameters.integrator_params['timestep'])
        integrator = integrators.LangevinIntegrator(temperature=self.config.parameters.integrator_params['temperature'],
                                                    timestep=self.config.parameters.integrator_params['timestep'],
                                                    collision_rate=self.config.parameters.integrator_params[
                                                        'collision_rate'],
                                                    constraint_tolerance=self.config.parameters.integrator_setConstraintTolerance)

        # prepare simulation
        prior_sim_vel = None
        if prior_sim is not None:
            prior_sim_vel = prior_sim.context.getState(getVelocities=True).getVelocities(asNumpy=True)
            del prior_sim
        self.simulation = app.Simulation(self.config.systemloader.get_topology(), system, integrator,
                                         self.config.parameters.platform, self.config.parameters.platform_config)
        self.simulation.context.setPositions(self.config.systemloader.get_positions())

        self.simulation.minimizeEnergy(self.config.parameters.minMaxIters)
        protein_index = set(self.config.systemloader.get_selection_protein())
        system.getMolecules()
        if prior_sim_vel is not None:
            self.simulation.context.setVelocitiesToTemperature(self.config.parameters.integrator_params['temperature'])
            cur_vel = self.simulation.context.getState(getVelocities=True).getVelocities(asNumpy=True)
            for i in protein_index:
                cur_vel[i] = prior_sim_vel[i]
            self.simulation.context.setVelocities(cur_vel)
        else:
            self.simulation.context.setVelocitiesToTemperature(self.config.parameters.integrator_params['temperature'])

    def translate(self, x, y, z, ligand_only=None, minimize=True):
        """

        :param x:
        :param y:
        :param z:
        :param minimize:
        """
        pos = self.simulation.context.getState(getPositions=True, getVelocities=True)
        pos = pos.getPositions(asNumpy=True)

        if ligand_only is None:
            pos += np.array([x, y, z]) * unit.angstrom
        else:
            pos[ligand_only] += np.array([x, y, z]) * unit.angstrom

        if minimize:
            self.simulation.minimizeEnergy()
            self.simulation.context.setVelocitiesToTemperature(self.config.parameters.integrator_params['temperature'])

    def get_sim_time(self):
        return self.config.parameters.integrator_params['timestep']

    def run(self, steps):
        """

        :param steps:
        """
        self.simulation.step(steps)

    def get_coordinates(self):
        """

        :return:
        """
        return self.simulation.context.getState(getPositions=True).getPositions(asNumpy=True)

    def get_pdb(self, file_name=None):
        """

        :return:
        """
        if file_name is None:
            output = StringIO()
        else:
            output = open(file_name, 'w')

        app.PDBFile.writeFile(self.simulation.topology,
                              self.simulation.context.getState(getPositions=True).getPositions(),
                              file=output)
        if file_name is None:
            return output.getvalue()
        else:
            output.close()
            return True

    def get_enthalpies(self, groups=None):
        return self.simulation.context.getState(getEnergy=True, groups=groups).getPotentialEnergy().value_in_unit(
            unit.kilojoule / unit.mole)
