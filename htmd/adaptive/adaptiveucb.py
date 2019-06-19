from os import path, makedirs
import numpy as np
from htmd.adaptive.adaptive import AdaptiveBase
from protocolinterface import val
from htmd.projections.tica import TICA
from htmd.projections.metric import Metric
from htmd.clustering.regular import RegCluster
from htmd.adaptive.util import getParentSimIdxFrame, updatingMean
from sklearn.cluster import MiniBatchKMeans
import logging
logger = logging.getLogger(__name__)


class AdaptiveUCB(AdaptiveBase):
    def __init__(self):
        from sklearn.base import ClusterMixin
        from moleculekit.projections.projection import Projection
        super().__init__()
        self._arg('datapath', 'str', 'The directory in which the completed simulations are stored', 'data', val.String())
        self._arg('filter', 'bool', 'Enable or disable filtering of trajectories.', True, val.Boolean())
        self._arg('filtersel', 'str', 'Filtering atom selection', 'not water', val.String())
        self._arg('filteredpath', 'str', 'The directory in which the filtered simulations will be stored', 'filtered', val.String())
        self._arg('projection', ':class:`Projection <moleculekit.projections.projection.Projection>` object',
                  'A Projection class object or a list of objects which will be used to project the simulation '
                   'data before constructing a Markov model', None, val.Object(Projection), nargs='+')
        self._arg('goalfunction', 'function',
                  'This function will be used to convert the goal-projected simulation data to a ranking which'
                  'can be used for the directed component of FAST.', None, val.Function(), nargs='any')
        self._arg('reward_method', 'str', 'The reward method', 'max', val.String())
        self._arg('skip', 'int', 'Allows skipping of simulation frames to reduce data. i.e. skip=3 will only keep every third frame', 1, val.Number(int, 'POS'))
        self._arg('lag', 'int', 'The lagtime used to create the Markov model', 1, val.Number(int, 'POS'))
        self._arg('exploration', 'float', 'Exploration is the coefficient used in UCB algorithm to weight the exploration value', 0.5, val.Number(float, 'OPOS'))
        self._arg('temperature', 'int', 'Temperature used to compute the free energy', 300, val.Number(int, 'POS'))
        self._arg('ticalag', 'int', 'Lagtime to use for TICA in frames. When using `skip` remember to change this accordinly.', 20, val.Number(int, '0POS'))
        self._arg('ticadim', 'int', 'Number of TICA dimensions to use. When set to 0 it disables TICA', 3, val.Number(int, '0POS'))
        self._arg('clustmethod', ':class:`ClusterMixin <sklearn.base.ClusterMixin>` class', 'Clustering algorithm used to cluster the contacts or distances', MiniBatchKMeans, val.Class(ClusterMixin))
        self._arg('macronum', 'int', 'The number of macrostates to produce', 8, val.Number(int, 'POS'))
        self._arg('save', 'bool', 'Save the model generated', False, val.Boolean())
        self._arg('save_qval', 'bool', 'Save the Q(a) and N values for every epoch', False, val.Boolean())
        self._arg('actionspace', 'str', 'The action space', 'metric', val.String())
        self._arg('recluster', 'bool', 'If to recluster the action space.', False, val.Boolean())
        self._arg('reclusterMethod', '', 'Clustering method for reclustering.', MiniBatchKMeans)
        self._arg('random', 'bool', 'Random decision mode for baseline.', False, val.Boolean())
        self._arg('reward_mode', 'str', '(parent, frame)', 'parent', val.String())
        self._arg('reward_window', 'int', 'The reward window', None, val.Number(int, 'POS'))
        self._arg('pucb', 'bool', 'If True, it uses PUCB algorithm using the provided goal function as a prior', False, val.Boolean())
        self._arg('goal_init', 'float', 'The proportional ratio of goal initialization compared to max frames set by nframes', 0.3, val.Number(float, 'POS'))
        self._arg('goal_preprocess', 'function',
                  'This function will be used to preprocess goal data after it has been computed for all frames.', None, val.Function(), nargs='any')
        self._arg('actionpool', 'int', 'The number of top scoring actions used to randomly select respawning simulations', 0, val.Number(int, 'OPOS'))

    def _algorithm(self):
        from htmd.kinetics import Kinetics
        sims = self._getSimlist()
        metr = Metric(sims, skip=self.skip)
        metr.set(self.projection)

        data = metr.project()
        data.dropTraj()  # Drop before TICA to avoid broken trajectories

        if self.goalfunction is not None:
            goaldata = self._getGoalData(data.simlist)
            if len(data.simlist) != len(goaldata.simlist):
                raise RuntimeError('The goal function was not able to project all trajectories that the MSM projection could. Check for possible errors in the goal function.')
            goaldataconcat = np.concatenate(goaldata.dat)
            if self.save:
                makedirs('saveddata', exist_ok=True)
                goaldata.save(path.join('saveddata', 'e{}_goaldata.dat'.format(self._getEpoch())))

        # tica = TICA(metr, int(max(2, np.ceil(self.ticalag))))  # gianni: without project it was tooooo slow
        if self.ticadim > 0:
            ticalag = int(np.ceil(max(2, min(np.min(data.trajLengths) / 2, self.ticalag))))  # 1 < ticalag < (trajLen / 2)
            tica = TICA(data, ticalag)
            datatica = tica.project(self.ticadim)
            if not self._checkNFrames(datatica): return False
            self._createMSM(datatica)
        else:
            if not self._checkNFrames(data): return False
            self._createMSM(data)

        confstatdist = self.conformationStationaryDistribution(self._model)
        if self.actionspace == 'metric':
            if not data.K:
                data.cluster(self.clustmethod(n_clusters=self._numClusters(data.numFrames)))
            data_q = data.copy()
        elif self.actionspace == 'goal':
            data_q = goaldata.copy()
        elif self.actionspace == 'tica':
            data_q = datatica.copy()
        elif self.actionspace == 'ticapcca':
            data_q = datatica.copy()
            for traj in data_q.trajectories:
                traj.cluster = self._model.macro_ofcluster[traj.cluster]
            data_q.K = self._model.macronum

        if self.recluster:
            print('Reclustering with {}'.format(self.reclusterMethod))
            data_q.cluster(self.reclusterMethod)
        
        numstates = data_q.K
        print('Numstates: {}'.format(numstates))
        currepoch = self._getEpoch()
        q_values = np.zeros(numstates, dtype=np.float32)
        n_values = np.zeros(numstates, dtype=np.int32)

        if self.random:  # If random mode respawn from random action states
            action_sel = np.zeros(numstates, dtype=int)
            N = self.nmax - self._running
            randomactions = np.bincount(np.random.randint(numstates, size=N))
            action_sel[:len(randomactions)] = randomactions
            if self.save_qval:
                makedirs('saveddata', exist_ok=True)
                np.save(path.join('saveddata', 'e{}_actions.npy'.format(currepoch)), action_sel)
            relFrames = self._getSpawnFrames_UCB(action_sel, data_q)
            self._writeInputs(data.rel2sim(np.concatenate(relFrames)))
            return True

        if self.goalfunction is not None:
            ## For every cluster in data_q, get the max score and initialize
            if self.goal_preprocess is not None:
                goaldataconcat = self.goal_preprocess(goaldataconcat)
            qstconcat = np.concatenate(data_q.St)
            statemaxes = np.zeros(numstates)
            np.maximum.at(statemaxes, qstconcat, np.squeeze(goaldataconcat))
            if not self.pucb:
                goalenergies = -Kinetics._kB * self.temperature * np.log(1-statemaxes)
                q_values = goalenergies
                n_values += int((self.nframes / self._numClusters(self.nframes)) * self.goal_init) ## Needs nframes to be set properly!!!!!!!!

        rewardtraj = np.arange(data_q.numTrajectories) # Recalculate reward for all states
        rewards = self.getRewards(rewardtraj, data_q, confstatdist, numstates, self.reward_method, self.reward_mode, self.reward_window)
        for i in range(numstates):
            if len(rewards[i]) == 0:
                continue
            q_values[i] = updatingMean(q_values[i], n_values[i], rewards[i])
        n_values += np.array([len(x) for x in rewards])


        if self.save_qval:
            makedirs('saveddata', exist_ok=True)
            np.save(path.join('saveddata', 'e{}_qval.npy'.format(currepoch)), q_values)
            np.save(path.join('saveddata', 'e{}_nval.npy'.format(currepoch)), n_values)

        
        if self.pucb:
            ucb_values = np.array([self.count_pucb(q_values[clust], self.exploration, statemaxes[clust], currepoch + 1, n_values[clust]) for clust in range(numstates)])
        else:
            ucb_values = np.array([self.count_ucb(q_values[clust], self.exploration, currepoch + 1, n_values[clust]) for clust in range(numstates)])

        if self.save_qval:
            makedirs('saveddata', exist_ok=True)
            np.save(path.join('saveddata', 'e{}_ucbvals.npy'.format(currepoch)), ucb_values)

        N = self.nmax - self._running
        if self.actionpool <= 0:
            self.actionpool = N
       
        topactions = np.argsort(-ucb_values)[:self.actionpool]
        action = np.random.choice(topactions, N, replace=False)

        action_sel = np.zeros(numstates, dtype=int)
        action_sel[action] += 1
        while np.sum(action_sel) < N:  # When K is lower than N repeat some actions
            for a in action:
                action_sel[a] +=1
                if np.sum(action_sel) == N:
                    break

        if self.save_qval:
            np.save(path.join('saveddata', 'e{}_actions.npy'.format(currepoch)), action_sel)
        relFrames = self._getSpawnFrames_UCB(action_sel, data_q) 
        self._writeInputs(data.rel2sim(np.concatenate(relFrames)))
        return True

    def _getSimlist(self):
        from glob import glob
        from htmd.simlist import simlist, simfilter
        logger.info('Postprocessing new data')

        sims = simlist(glob(path.join(self.datapath, '*', '')), glob(path.join(self.inputpath, '*', '')),
                       glob(path.join(self.inputpath, '*', '')))

        if self.filter:
            sims = simfilter(sims, self.filteredpath, filtersel=self.filtersel)
        return sims


    def count_ucb(self, q_value, exploration, step, n_value):
        return (q_value + (exploration * np.sqrt((np.log(step) / (n_value + 1)))))

    def count_pucb(self, q_value, exploration, predictor, step, n_value):
        return (q_value + (exploration * predictor * np.sqrt((np.log(step) / (n_value + 1)))))

    def getRewards(self, trajidx, data_q, confstatdist, numstates, rewardmethod, rewardmode, rewardwindow):
        from htmd.kinetics import Kinetics
        import pandas as pd
        rewards = [[] for _ in range(numstates)]
        for simidx in trajidx:
            # Get the eq distribution of each of the states the sim passed through
            states = data_q.St[simidx]
            statprob = confstatdist[simidx]
            connected = (states != -1) & (statprob != 0)
            if not np.any(connected):
                continue
            states = states[connected]
            statprob = statprob[connected]
            #energies = Kinetics._kB * self.temperature * np.log(statprob)
            energies = -Kinetics._kB * self.temperature * np.log(1-statprob)
            ww = rewardwindow
            if rewardwindow is None:
                ww = len(energies)

            if rewardmethod == 'mean':
                windowedreward = pd.Series(energies[::-1]).rolling(ww, min_periods=1).mean().values[::-1]
            elif rewardmethod == 'max':
                windowedreward = pd.Series(energies[::-1]).rolling(ww, min_periods=1).max().values[::-1]
            else:
                raise RuntimeError('Reward method {} not available'.format(rewardmethod))

            if rewardmode == 'parent':
                # Get the state of the conformation from which the sim was spawned
                parentidx, parentframe = getParentSimIdxFrame(data_q, simidx)
                if parentidx == -1:  # Parent frame doesn't belong to any state
                    print('Parent frame doesn\'t belong to any state')
                    continue
                prev_action = data_q.St[parentidx][parentframe]
                rewards[prev_action].append(windowedreward[0])
            elif rewardmode == 'frames':
                for st, re in zip(states, windowedreward):
                    rewards[st].append(re)
            else:
                raise RuntimeError('Invalid reward mode {}'.format(rewardmode))

        return rewards

    def conformationStationaryDistribution(self, model):
        statdist = np.zeros(model.data.numFrames) # zero for disconnected set
        dataconcatSt = np.concatenate(model.data.St)
        for i in range(model.micronum):
            microframes = np.where(model.micro_ofcluster[dataconcatSt] == i)[0]
            statdist[microframes] = model.msm.stationary_distribution[i]
        return model.data.deconcatenate(statdist)

    def _checkNFrames(self, data):
        if self.nframes != 0 and data.numFrames >= self.nframes:
            logger.info('Reached maximum number of frames. Stopping adaptive.')
            return False
        return True

    def _getGoalData(self, sims):
        from htmd.projections.metric import Metric
        logger.debug('Starting projection of directed component')
        metr = Metric(sims, skip=self.skip)
        metr.set(self.goalfunction)
        data = metr.project()
        logger.debug('Finished calculating directed component')
        return data

    def _createMSM(self, data):
        kmeanserror = True
        while kmeanserror:
            try:
                data.cluster(self.clustmethod(n_clusters=self._numClusters(data.numFrames)))
            except IndexError:
                continue
            kmeanserror = False
            
        self._model = Model(data)
        self._model.markovModel(self.lag, self._numMacrostates(data))
        if self.save:
            makedirs('saveddata', exist_ok=True)
            self._model.save(path.join('saveddata', 'e{}_adapt_model.dat'.format(self._getEpoch())))

    def _getSpawnFrames_UCB(self, reward, data):
        stateIdx = np.where(reward > 0)[0]
        _, relFrames = data.sampleClusters(stateIdx, reward[stateIdx], replacement=True, allframes=False)
        logger.debug('relFrames {}'.format(relFrames))
        return relFrames

    def _numClusters(self, numFrames):
        """ Heuristic that calculates number of clusters from number of frames """
        K = int(max(np.round(0.6 * np.log10(numFrames / 1000) * 1000 + 50), 100))  # heuristic
        if K > numFrames / 3:  # Ugly patch for low-data regimes ...
            K = int(numFrames / 3)
        return K

    def _numMacrostates(self, data):
        """ Heuristic for calculating the number of macrostates for the Markov model """
        macronum = self.macronum
        if data.K < macronum:
            macronum = np.ceil(data.K / 2)
            logger.warning('Using less macrostates than requested due to lack of microstates. macronum = ' + str(macronum))

        # Calculating how many timescales are above the lag time to limit number of macrostates
        from pyemma.msm import timescales_msm
        timesc = timescales_msm(data.St.tolist(), lags=self.lag, nits=macronum).get_timescales()
        macronum = min(self.macronum, max(np.sum(timesc > self.lag), 2))
        return macronum


from unittest import TestCase
class _TestAdaptiveUCB(TestCase):
    @classmethod
    def setUpClass(self):
        from htmd.util import tempname
        from htmd.home import home
        from moleculekit.projections.metricdistance import MetricDistance

        tmpdir = tempname()
        shutil.copytree(home(dataDir='adaptive'), tmpdir)
        os.chdir(tmpdir)

    def test_adaptive(self):
        ad = AdaptiveUCB()
