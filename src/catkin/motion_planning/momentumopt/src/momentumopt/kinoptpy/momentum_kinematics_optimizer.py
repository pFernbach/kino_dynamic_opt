import os
import numpy as np

from momentumopt.kinoptpy.qp import QpSolver
from momentumopt.kinoptpy.inverse_kinematics import PointContactInverseKinematics
from pinocchio import RobotWrapper
import pinocchio as se3
from pinocchio.utils import zero
from pymomentum import *

from quadruped.quadruped_wrapper import QuadrupedWrapper
from momentumopt.kinoptpy.min_jerk_traj import *

from pymomentum import \
    PlannerVectorParam_KinematicDefaultJointPositions, \
    PlannerIntParam_NumTimesteps, \
    PlannerDoubleParam_TimeStep

class Contact(object):
    def __init__(self, position, start_time, end_time):
        self.pos = position
        self.init_time = start_time
        self.final_time = end_time

    def position(self):
        return self.pos

    def start_time(self):
        return self.init_time

    def end_time(self):
        return self.final_time


def get_contact_plan(contact_states, effs):
    contacts = {}
    for i, eff in enumerate(effs):
        num_contacts = len(contact_states(i))
        contacts[eff] = []
        for j in range(num_contacts):
            contact_ = contact_states(i)[j]
            start_time = contact_.start_time
            end_time = contact_.end_time
            position = contact_.position
            contacts[eff].append(Contact(position, start_time, end_time))

    return contacts


def generate_eff_traj(contacts, z_offset):
    effs = contacts.keys()
    eff_traj_poly = {}

    for eff in effs:
        cnt = contacts[eff]
        num_contacts = len(cnt)

        poly_traj = [
            PolynominalList(), PolynominalList(), PolynominalList()
        ]

        for i in range(num_contacts):
            # Create a constant polynominal for endeffector on the ground.
            t = [cnt[i].start_time(), cnt[i].end_time()]
            for idx in range(3):
                poly_traj[idx].append(t, constant_poly(cnt[i].position()[idx]))

            # If there is a contact following, add the transition between
            # the two contact points.
            if i < num_contacts - 1:
                t = [cnt[i].end_time(), cnt[i+1].start_time()]

                for idx in range(3):
                    via = None
                    if idx == 2:
                        via = z_offset + cnt[i].position()[idx]
                    poly = poly_points(t, cnt[i].position()[idx], cnt[i+1].position()[idx], via)
                    poly_traj[idx].append(t, poly)

        eff_traj_poly[eff] = poly_traj

    # returns end eff trajectories
    return eff_traj_poly


class EndeffectorTrajectoryGenerator(object):
    def __init__(self):
        self.z_offset = 0.1

    def get_z_bound(self, mom_kin_optimizer):
        z_max = min(max(mom_kin_optimizer.com_dyn[:, 2]), self.max_bound)
        z_min = max(min(mom_kin_optimizer.com_dyn[:, 2]), self.min_bound)
        return z_max, z_min

    def __call__(self, mom_kin_optimizer):
        """
        Computes the endeffector positions and velocities.

        Returns endeff_pos_ref, endeff_vel_ref
            [0]: endeff_pos_ref: np.array, shape=[num_time_steps, num_eff, 3={x, y, z}]
            [1]: endeff_vel_ref: np.array, shape=[num_time_steps, num_eff, 3={x, y, z}]
        """
        dt = mom_kin_optimizer.dt
        num_eff = len(mom_kin_optimizer.eff_names)
        num_time_steps = mom_kin_optimizer.num_time_steps

        contacts = get_contact_plan(mom_kin_optimizer.contact_sequence.contact_states,
                                    mom_kin_optimizer.eff_names)

        # Generate minimum jerk trajectories
        eff_traj_poly = generate_eff_traj(contacts, self.z_offset)

        # Compute the endeffector position and velocity trajectories.
        endeff_pos_ref = np.zeros((num_time_steps, num_eff, 3))
        endeff_vel_ref = np.zeros((num_time_steps, num_eff, 3))
        endeff_contact = np.zeros((num_time_steps, num_eff))

        for it in range(num_time_steps):
            for eff, name in enumerate(mom_kin_optimizer.eff_names):
                endeff_pos_ref[it][eff] = [eff_traj_poly[name][i].eval(it * dt) for i in range(3)]
                endeff_vel_ref[it][eff] = [eff_traj_poly[name][i].deval(it * dt) for i in range(3)]

                # HACK: If the velocity is zero, assume the endeffector is in
                # contact with the ground.
                if np.all(endeff_vel_ref[it][eff] == 0.):
                    endeff_contact[it][eff] = 1.
                else:
                    endeff_contact[it][eff] = 0.

        return endeff_pos_ref, endeff_vel_ref, endeff_contact


class MomentumKinematicsOptimizer(object):
    def __init__(self):
        self.q_init = None
        self.dq_init = None
        self.reg_orientation = 1e-2

    def reset(self):
        self.kinematics_sequence = KinematicsSequence()
        self.kinematics_sequence.resize(self.planner_setting.get(PlannerIntParam_NumTimesteps),
                                        self.planner_setting.get(PlannerIntParam_NumDofs))

    def initialize(self, planner_setting, max_iterations=50, eps=0.001, endeff_traj_generator=None):
        self.planner_setting = planner_setting

        if endeff_traj_generator is None:
            endeff_traj_generator = EndeffectorTrajectoryGenerator()
        self.endeff_traj_generator = endeff_traj_generator

        self.dt = planner_setting.get(PlannerDoubleParam_TimeStep)
        self.num_time_steps = planner_setting.get(PlannerIntParam_NumTimesteps)

        self.max_iterations = max_iterations
        self.eps = eps

        # Load the robot from URDF-model
        urdf = str(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) + '/urdf/quadruped.urdf')
        # print("urdf_path:", urdf)
        self.robot = QuadrupedWrapper(urdf)

        self.reset()

        # Holds dynamics and kinematics results
        self.com_dyn = np.zeros((self.num_time_steps, 3))
        self.lmom_dyn = np.zeros((self.num_time_steps, 3))
        self.amom_dyn = np.zeros((self.num_time_steps, 3))

        self.com_kin = np.zeros((self.num_time_steps, 3))
        self.lmom_kin = np.zeros((self.num_time_steps, 3))
        self.amom_kin = np.zeros((self.num_time_steps, 3))
        self.q_kin = np.zeros((self.num_time_steps, self.robot.model.nq))
        self.dq_kin = np.zeros((self.num_time_steps, self.robot.model.nv))

        self.eff_names = ['{}_END'.format(eff) for eff in self.robot.effs]
        self.inv_kin = PointContactInverseKinematics(self.robot.model, self.eff_names)

        self.motion_eff = {}

    def fill_data_from_dynamics(self):
        # The centroidal information
        for it in range(self.num_time_steps):
          self.com_dyn[it] = self.dynamic_sequence.dynamics_states[it].com
          self.lmom_dyn[it] = self.dynamic_sequence.dynamics_states[it].lmom
          self.amom_dyn[it] = self.dynamic_sequence.dynamics_states[it].amom

    def fill_endeffector_trajectory(self):
        self.endeff_pos_ref, self.endeff_vel_ref, self.endeff_contact = \
                self.endeff_traj_generator(self)

    def fill_kinematic_result(self, it, q, dq):
        hg = self.inv_kin.robot.centroidalMomentum(q, dq)

        # Storing on the internal array.
        self.com_kin[it] = self.inv_kin.robot.com().T
        self.lmom_kin[it] = hg.linear.T
        self.amom_kin[it] = hg.angular.T
        self.q_kin[it] = q.T
        self.dq_kin[it] = dq.T

        # Storing on the kinematic sequence.
        kinematic_state = self.kinematics_sequence.kinematics_states[it]
        kinematic_state.com = self.com_kin[it]
        kinematic_state.lmom = self.lmom_kin[it]
        kinematic_state.amom = self.amom_kin[it]

        kinematic_state.robot_posture.base_position = q[:3]
        kinematic_state.robot_posture.base_orientation = q[3:7]
        kinematic_state.robot_posture.joint_positions = q[7:]

        kinematic_state.robot_velocity.base_linear_velocity = dq[:3]
        kinematic_state.robot_velocity.base_angular_velocity = dq[3:6]
        kinematic_state.robot_velocity.joint_velocities = dq[6:]


    def optimize_initial_position(self, init_state):
        # Optimize the initial configuration
        q = self.robot.model.neutralConfiguration.copy()
        q[7:] = np.matrix(
            self.planner_setting.get(PlannerVectorParam_KinematicDefaultJointPositions)).T
        dq = np.matrix(np.zeros(self.robot.robot.nv)).T

        com_ref = init_state.com
        lmom_ref = np.zeros(3)
        amom_ref = np.zeros(3)
        endeff_pos_ref = np.array([init_state.effPosition(i) for i in range(init_state.effNum())])
        endeff_vel_ref = np.matrix(np.zeros((init_state.effNum(), 3)))
        endeff_contact = np.ones(init_state.effNum())
        quad_goal = se3.Quaternion(se3.rpy.rpyToMatrix(np.matrix([0.0, 0, 0.]).T))
        q[3:7] = quad_goal.coeffs()

        for iters in range(self.max_iterations):
            # Adding small P controller for the base orientation to always start with flat
            # oriented base.
            quad_q = se3.Quaternion(float(q[6]), float(q[3]), float(q[4]), float(q[5]))
            amom_ref = 1e-2 * se3.log((quad_goal * quad_q.inverse()).matrix())

            res = self.inv_kin.compute(q, dq, com_ref, lmom_ref, amom_ref,
                                      endeff_pos_ref, endeff_vel_ref, endeff_contact)
            q = se3.integrate(self.robot.model, q, res)
            self.robot.display(q)

            if np.linalg.norm(res) < 1e-5:
                print('Found initial configuration after {} iterations'.format(iters + 1))
                break

        if iters == self.max_iterations - 1:
            print('Failed to converge for initial setup.')

        self.q_init = q.copy()
        self.dq_init = dq.copy()

    def optimize(self, init_state, contact_sequence, dynamic_sequence, plotting=False):
        self.init_state = init_state
        self.contact_sequence = contact_sequence
        self.dynamic_sequence = dynamic_sequence

        # Create array with centroidal and endeffector informations.
        self.fill_data_from_dynamics()
        self.fill_endeffector_trajectory()

        # Run the optimization for the initial configuration only once.
        if self.q_init is None:
            self.optimize_initial_position(init_state)

        # Compute inverse kinematics over the full trajectory.
        q, dq = self.q_init.copy(), self.dq_init.copy()
        for it in range(self.num_time_steps):
            quad_goal = se3.Quaternion(se3.rpy.rpyToMatrix(np.matrix([0.0, 0, 0.]).T))
            quad_q = se3.Quaternion(float(q[6]), float(q[3]), float(q[4]), float(q[5]))
            amom_ref = (self.reg_orientation * se3.log((quad_goal * quad_q.inverse()).matrix()).T + self.amom_dyn[it]).reshape(-1)

            dq = self.inv_kin.compute(
                    q, dq, self.com_dyn[it], self.lmom_dyn[it], amom_ref,
                    self.endeff_pos_ref[it], self.endeff_vel_ref[it],
                    self.endeff_contact[it])

            # Fill the kinematics results for it.
            self.fill_kinematic_result(it, q, dq)

            # Integrate to the next state.
            q = se3.integrate(self.robot.model, q, dq * self.dt)