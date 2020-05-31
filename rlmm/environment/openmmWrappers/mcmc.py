import mdtraj as md
import numpy as np
from openmmtools import cache
from openmmtools.mcmc import MCMCSampler
from openmmtools.states import ThermodynamicState, SamplerState
from simtk import unit
from tqdm import tqdm

import rlmm.environment.openmmWrappers.utils as mmWrapperUtils
from rlmm.utils.config import Config
from rlmm.utils.loggers import DCDReporter
from rlmm.utils.loggers import make_message_writer


class MCMCOpenMMSimulationWrapper:
    class Config(Config):
        def __init__(self, args):
            self.hybrid = None
            self.ligand_pertubation_samples = None
            self.displacement_sigma = None
            self.verbose = None
            self.n_steps = None
            self.parameters = mmWrapperUtils.SystemParams(args['params'])
            self.warmupparameters = None
            if "warmupparams" in args:
                self.warmupparameters = mmWrapperUtils.SystemParams(args['warmupparams'])

            self.systemloader = None
            if args is not None:
                self.__dict__.update(args)

        def get_obj(self, system_loader, *args, **kwargs):
            self.systemloader = system_loader
            return MCMCOpenMMSimulationWrapper(self, *args, **kwargs)

    def __init__(self, config_: Config, old_sampler_state=None):
        """

        :param systemLoader:
        :param config:
        """
        self._times = None
        self.config = config_
        self.logger = make_message_writer(self.config.verbose, self.__class__.__name__)
        with self.logger("__init__") as logger:
            self.explicit = self.config.systemloader.explicit
            self.amber = bool(self.config.systemloader.config.method == 'amber')
            self._trajs = np.zeros((1, 1))
            self._id_number = int(self.config.systemloader.params_written)

            if self.config.systemloader.system is None:
                self.system = self.config.systemloader.get_system(self.config.parameters.createSystem)
                self.topology = self.config.systemloader.topology

                cache.global_context_cache.set_platform(self.config.parameters.platform,
                                                        self.config.parameters.platform_config)
                prot_atoms = None

                positions, velocities = self.config.systemloader.get_positions(), None

            else:
                self.system = self.config.systemloader.system
                self.topology = self.config.systemloader.topology
                past_sampler_state_velocities = old_sampler_state.sampler.sampler_state.velocities
                prot_atoms = md.Topology.from_openmm(self.topology).select("protein")
                positions, velocities = self.config.systemloader.get_positions(), None

            sequence_move = mmWrapperUtils.prepare_mcmc(self.topology, self.config)

            self.sampler = MCMCSampler(ThermodynamicState(system=self.system,
                                                          temperature=self.config.parameters.integrator_params[
                                                              'temperature'],
                                                          pressure=1.0 * unit.atmosphere if self.config.systemloader.explicit else None)
                                       , SamplerState(positions=positions, velocities=velocities,
                                                      box_vectors=self.config.systemloader.boxvec), move=sequence_move)
            self.sampler.minimize(max_iterations=self.config.parameters.minMaxIters)

            # set velocities from temperature
            ctx = cache.global_context_cache.get_context(self.sampler.thermodynamic_state)[0]
            ctx.setVelocitiesToTemperature(self.config.parameters.integrator_params[
                                                              'temperature'])
            self.sampler.sampler_state.velocities = ctx.getState(getVelocities=True).getVelocities()

            # reassign protein velocities from prior simulation
            if prot_atoms is not None:
                velocities = self.sampler.sampler_state.velocities
                for prot_atom in prot_atoms:
                    velocities[prot_atom] = past_sampler_state_velocities[prot_atom]
                self.sampler.sampler_state.velocities = velocities

    def run(self, iters, steps_per_iter):
        """

        :param steps:
        """
        with self.logger("run") as logger:

            if 'cur_sim_steps' not in self.__dict__:
                self.cur_sim_steps = 0.0 * unit.picosecond

            pbar = tqdm(range(iters), desc="running {} steps per sample".format(steps_per_iter))
            self._trajs = np.zeros((iters, self.system.getNumParticles(), 3))
            self._times = np.zeros((iters))
            dcdreporter = DCDReporter(f"{self.config.tempdir()}/traj.dcd", 1, append=False)
            for i in pbar:
                self.sampler.run(steps_per_iter)
                self.cur_sim_steps += (steps_per_iter * self.get_sim_time())
                _state = cache.global_context_cache.get_context(self.sampler.thermodynamic_state)[0].getState(
                    getPositions=True)
                dcdreporter.report(self.topology, _state, (i + 1), 0.5 * unit.femtosecond)

                # log trajectory
                self._trajs[i] = np.array(self.sampler.sampler_state.positions.value_in_unit(unit.angstrom)).reshape(
                    (self.system.getNumParticles(), 3))
                self._times[i] = self.cur_sim_steps.value_in_unit(unit.picosecond)
            pbar.close()

    def writetraj(self):
        if self.explicit:
            lengths, angles = mmWrapperUtils.get_mdtraj_box(boxvec=self.sampler.sampler_state.box_vectors,
                                                            iterset=self._trajs.shape[0])
            traj = md.Trajectory(self._trajs, md.Topology.from_openmm(self.topology),
                                 unitcell_lengths=lengths,
                                 unitcell_angles=angles, time=self._times)
            traj.image_molecules(inplace=True)
        else:
            traj = md.Trajectory(self._trajs, md.Topology.from_openmm(self.topology), time=self._times)

        traj.save_hdf5(f'{self.config.tempdir()}/mdtraj_traj.h5')
        return traj

    def run_amber_mmgbsa(self, run_decomp=False):
        return mmWrapperUtils.run_amber_mmgbsa(self.logger, self.explicit, self.config.tempdir(), run_decomp=run_decomp)

    def get_sim_time(self):
        return self.config.n_steps * self.config.parameters.integrator_params['timestep']

    def get_velocities(self):
        return self.sampler.sampler_state.velocities

    def get_coordinates(self):
        return mmWrapperUtils.get_coordinates_samplers(self.topology, self.sampler.sampler_state, self.explicit)

    def get_pdb(self, file_name=None):
        return mmWrapperUtils.get_pdb(self.topology, self.get_coordinates(), file_name=file_name)