# Delta calibration support
#
# Copyright (C) 2017-2018  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import math, logging, collections
import probe, mathutil


######################################################################
# Delta "stable position" coordinates
######################################################################

# A "stable position" is a 3-tuple containing the number of steps
# taken since hitting the endstop on each delta tower.  Delta
# calibration uses this coordinate system because it allows a position
# to be described independent of the software parameters.

# Storage helper for delta parameters
DeltaParams = collections.namedtuple('DeltaParams', [
    'radius', 'angles', 'arms', 'endstops', 'stepdists',
    'towers', 'abs_endstops'])

# Generate delta_params from delta configuration parameters
def build_delta_params(params):
    radius = params['radius']
    angles = [params['angle_'+a] for a in 'abc']
    arms = [params['arm_'+a] for a in 'abc']
    endstops = [params['endstop_'+a] for a in 'abc']
    stepdists = [params['stepdist_'+a] for a in 'abc']
    # Calculate the XY cartesian coordinates of the delta towers
    radian_angles = [math.radians(a) for a in angles]
    towers = [(math.cos(a) * radius, math.sin(a) * radius)
              for a in radian_angles]
    # Calculate the absolute Z height of each tower endstop
    radius2 = radius**2
    abs_endstops = [e + math.sqrt(a**2 - radius2)
                    for e, a in zip(endstops, arms)]
    return DeltaParams(radius, angles, arms, endstops, stepdists,
                       towers, abs_endstops)

# Return cartesian coordinates for the given stable_positions when the
# given delta_params are used.
def get_position_from_stable(stable_position, delta_params):
    dp = delta_params
    sphere_coords = [
        (t[0], t[1], es - sp * sd)
        for sd, t, es, sp in zip(
                dp.stepdists, dp.towers, dp.abs_endstops, stable_position) ]
    return mathutil.trilateration(sphere_coords, [a**2 for a in dp.arms])

# Return a stable position from a cartesian coordinate
def calc_stable_position(coord, delta_params):
    dp = delta_params
    steppos = [
        math.sqrt(a**2 - (t[0]-coord[0])**2 - (t[1]-coord[1])**2) + coord[2]
        for t, a in zip(dp.towers, dp.arms) ]
    return [(ep - sp) / sd
            for sd, ep, sp in zip(dp.stepdists, dp.abs_endstops, steppos)]

# Load a stable position from a config entry
def load_config_stable(config, option):
    spos = config.get(option)
    try:
        sa, sb, sc = map(float, spos.split(','))
    except:
        msg = "Unable to parse stable position '%s'" % (spos,)
        logging.exception(msg)
        raise config.error(msg)
    return sa, sb, sc


######################################################################
# Delta calibration object
######################################################################

# The angles and distances of the calibration object found in
# docs/prints/calibrate_size.stl
MeasureAngles = [210., 270., 330., 30., 90., 150.]
MeasureOuterRadius = 65
MeasureRidgeRadius = 5. - .5

# How much to prefer a distance measurement over a height measurement
MEASURE_WEIGHT = 0.5

# Convert distance measurements made on the calibration object to
# 3-tuples of (actual_distance, stable_position1, stable_position2)
def measurements_to_distances(measured_params, delta_params):
    # Extract params
    mp = measured_params
    dp = delta_params
    scale = mp['SCALE'][0]
    cpw = mp['CENTER_PILLAR_WIDTHS']
    center_widths = [cpw[0], cpw[2], cpw[1], cpw[0], cpw[2], cpw[1]]
    center_dists = [od - cw
                    for od, cw in zip(mp['CENTER_DISTS'], center_widths)]
    outer_dists = [
        od - opw
        for od, opw in zip(mp['OUTER_DISTS'], mp['OUTER_PILLAR_WIDTHS']) ]
    # Convert angles in degrees to an XY multiplier
    obj_angles = map(math.radians, MeasureAngles)
    xy_angles = zip(map(math.cos, obj_angles), map(math.sin, obj_angles))
    # Calculate stable positions for center measurements
    inner_ridge = MeasureRidgeRadius * scale
    inner_pos = [(ax * inner_ridge, ay * inner_ridge, 0.)
                 for ax, ay in xy_angles]
    outer_ridge = (MeasureOuterRadius + MeasureRidgeRadius) * scale
    outer_pos = [(ax * outer_ridge, ay * outer_ridge, 0.)
                 for ax, ay in xy_angles]
    center_positions = [
        (cd, calc_stable_position(ip, dp), calc_stable_position(op, dp))
        for cd, ip, op in zip(center_dists, inner_pos, outer_pos)]
    # Calculate positions of outer measurements
    outer_center = MeasureOuterRadius * scale
    start_pos = [(ax * outer_center, ay * outer_center) for ax, ay in xy_angles]
    shifted_angles = xy_angles[2:] + xy_angles[:2]
    first_pos = [(ax * inner_ridge + spx, ay * inner_ridge + spy, 0.)
                 for (ax, ay), (spx, spy) in zip(shifted_angles, start_pos)]
    second_pos = [(ax * outer_ridge + spx, ay * outer_ridge + spy, 0.)
                  for (ax, ay), (spx, spy) in zip(shifted_angles, start_pos)]
    outer_positions = [
        (od, calc_stable_position(fp, dp), calc_stable_position(sp, dp))
        for od, fp, sp in zip(outer_dists, first_pos, second_pos)]
    return center_positions + outer_positions


######################################################################
# Delta Calibrate class
######################################################################

class DeltaCalibrate:
    def __init__(self, config):
        self.printer = config.get_printer()
        if config.getsection('printer').get('kinematics') != 'delta':
            raise config.error("Delta calibrate is only for delta printers")
        # Calculate default probing points
        radius = config.getfloat('radius', above=0.)
        points = [(0., 0.)]
        scatter = [.95, .90, .85, .70, .75, .80]
        for i in range(6):
            r = math.radians(90. + 60. * i)
            dist = radius * scatter[i]
            points.append((math.cos(r) * dist, math.sin(r) * dist))
        self.probe_helper = probe.ProbePointsHelper(
            config, self.probe_finalize, default_points=points)
        # Restore probe stable positions
        self.last_probe_positions = []
        for i in range(999):
            height = config.getfloat("height%d" % (i,), None)
            if height is None:
                break
            height_pos = load_config_stable(config, "height%d_pos" % (i,))
            self.last_probe_positions.append((height, height_pos))
        # Restore distance measurements
        self.delta_analyze_entry = {'SCALE': (1.,)}
        self.last_distances = []
        for i in range(999):
            dist = config.getfloat("distance%d" % (i,), None)
            if dist is None:
                break
            distance_pos1 = load_config_stable(config, "distance%d_pos1" % (i,))
            distance_pos2 = load_config_stable(config, "distance%d_pos2" % (i,))
            self.last_distances.append((dist, distance_pos1, distance_pos2))
        # Register gcode commands
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command('DELTA_CALIBRATE', self.cmd_DELTA_CALIBRATE,
                                    desc=self.cmd_DELTA_CALIBRATE_help)
        self.gcode.register_command('DELTA_ANALYZE', self.cmd_DELTA_ANALYZE,
                                    desc=self.cmd_DELTA_ANALYZE_help)
    def save_state(self, probe_positions, distances, params):
        # Save main delta parameters
        configfile = self.printer.lookup_object('configfile')
        configfile.set('printer', 'delta_radius', "%.6f" % (params['radius']))
        for axis in 'abc':
            configfile.set('stepper_'+axis, 'angle',
                           "%.6f" % (params['angle_'+axis],))
            configfile.set('stepper_'+axis, 'arm_length',
                           "%.6f" % (params['arm_'+axis],))
            configfile.set('stepper_'+axis, 'position_endstop',
                           "%.6f" % (params['endstop_'+axis],))
        # Save probe stable positions
        section = 'delta_calibrate'
        configfile.remove_section(section)
        for i, (z_offset, spos) in enumerate(probe_positions):
            configfile.set(section, "height%d" % (i,), z_offset)
            configfile.set(section, "height%d_pos" % (i,),
                           "%.3f,%.3f,%.3f" % tuple(spos))
        # Save distance measurements
        for i, (dist, spos1, spos2) in enumerate(distances):
            configfile.set(section, "distance%d" % (i,), dist)
            configfile.set(section, "distance%d_pos1" % (i,),
                           "%.3f,%.3f,%.3f" % tuple(spos1))
            configfile.set(section, "distance%d_pos2" % (i,),
                           "%.3f,%.3f,%.3f" % tuple(spos2))
    def probe_finalize(self, offsets, positions):
        # Convert positions into (z_offset, stable_position) pairs
        z_offset = offsets[2]
        kin = self.printer.lookup_object('toolhead').get_kinematics()
        delta_params = build_delta_params(kin.get_calibrate_params())
        probe_positions = [(z_offset, calc_stable_position(p, delta_params))
                           for p in positions]
        # Perform analysis
        self.calculate_params(probe_positions, self.last_distances)
    def calculate_params(self, probe_positions, distances):
        # Setup for coordinate descent analysis
        kin = self.printer.lookup_object('toolhead').get_kinematics()
        params = kin.get_calibrate_params()
        orig_delta_params = build_delta_params(params)
        logging.info("Calculating delta_calibrate with:\n%s\n%s\n"
                     "Initial delta_calibrate parameters: %s",
                     probe_positions, distances, params)
        adj_params = ('radius', 'angle_a', 'angle_b',
                      'endstop_a', 'endstop_b', 'endstop_c')
        z_weight = 1.
        if distances:
            adj_params += ('arm_a', 'arm_b', 'arm_c')
            z_weight = len(distances) / (MEASURE_WEIGHT * len(probe_positions))
        # Perform coordinate descent
        call_count = [0]
        def delta_errorfunc(params):
            call_count[0] += 1
            if not call_count[0] % 1000:
                self.gcode.respond_info("Working on calibration...")
                self.printer.get_reactor().pause(0.)
            # Build new delta_params for params under test
            delta_params = build_delta_params(params)
            # Calculate z height errors
            total_error = 0.
            for z_offset, stable_pos in probe_positions:
                x, y, z = get_position_from_stable(stable_pos, delta_params)
                total_error += (z - z_offset)**2
            total_error *= z_weight
            # Calculate distance errors
            for dist, stable_pos1, stable_pos2 in distances:
                x1, y1, z1 = get_position_from_stable(stable_pos1, delta_params)
                x2, y2, z2 = get_position_from_stable(stable_pos2, delta_params)
                d = math.sqrt((x1-x2)**2 + (y1-y2)**2 + (z1-z2)**2)
                total_error += (d - dist)**2
            return total_error
        new_params = mathutil.coordinate_descent(
            adj_params, params, delta_errorfunc)
        # Log and report results
        logging.info("Calculated delta_calibrate parameters: %s", new_params)
        new_delta_params = build_delta_params(new_params)
        for z_offset, spos in probe_positions:
            logging.info("height orig: %.6f new: %.6f goal: %.6f",
                         get_position_from_stable(spos, orig_delta_params)[2],
                         get_position_from_stable(spos, new_delta_params)[2],
                         z_offset)
        for dist, spos1, spos2 in distances:
            x1, y1, z1 = get_position_from_stable(spos1, orig_delta_params)
            x2, y2, z2 = get_position_from_stable(spos2, orig_delta_params)
            orig_dist = math.sqrt((x1-x2)**2 + (y1-y2)**2 + (z1-z2)**2)
            x1, y1, z1 = get_position_from_stable(spos1, new_delta_params)
            x2, y2, z2 = get_position_from_stable(spos2, new_delta_params)
            new_dist = math.sqrt((x1-x2)**2 + (y1-y2)**2 + (z1-z2)**2)
            logging.info("distance orig: %.6f new: %.6f goal: %.6f",
                         orig_dist, new_dist, dist)
        self.gcode.respond_info(
            "stepper_a: position_endstop: %.6f angle: %.6f arm: %.6f\n"
            "stepper_b: position_endstop: %.6f angle: %.6f arm: %.6f\n"
            "stepper_c: position_endstop: %.6f angle: %.6f arm: %.6f\n"
            "delta_radius: %.6f\n"
            "The SAVE_CONFIG command will update the printer config file\n"
            "with these parameters and restart the printer." % (
                new_params['endstop_a'], new_params['angle_a'],
                new_params['arm_a'],
                new_params['endstop_b'], new_params['angle_b'],
                new_params['arm_b'],
                new_params['endstop_c'], new_params['angle_c'],
                new_params['arm_c'],
                new_params['radius']))
        # Store results for SAVE_CONFIG
        self.save_state(probe_positions, distances, new_params)
    cmd_DELTA_CALIBRATE_help = "Delta calibration script"
    def cmd_DELTA_CALIBRATE(self, params):
        self.gcode.run_script_from_command("G28")
        self.probe_helper.start_probe(params)
    def do_extended_calibration(self):
        # Extract distance positions
        if len(self.delta_analyze_entry) <= 1:
            distances = self.last_distances
        elif len(self.delta_analyze_entry) < 5:
            raise self.gcode.error("Not all measurements provided")
        else:
            kin = self.printer.lookup_object('toolhead').get_kinematics()
            delta_params = build_delta_params(kin.get_calibrate_params())
            distances = measurements_to_distances(
                self.delta_analyze_entry, delta_params)
        if not self.last_probe_positions:
            raise self.gcode.error(
                "Must run basic calibration with DELTA_CALIBRATE first")
        # Perform analysis
        self.calculate_params(self.last_probe_positions, distances)
    cmd_DELTA_ANALYZE_help = "Extended delta calibration tool"
    def cmd_DELTA_ANALYZE(self, params):
        # Parse distance measurements
        args = {'CENTER_DISTS': 6, 'CENTER_PILLAR_WIDTHS': 3,
                'OUTER_DISTS': 6, 'OUTER_PILLAR_WIDTHS': 6, 'SCALE': 1}
        for name, count in args.items():
            if name not in params:
                continue
            data = self.gcode.get_str(name, params)
            try:
                parts = map(float, data.split(','))
            except:
                raise self.gcode.error("Unable to parse parameter '%s'" % (
                    name,))
            if len(parts) != count:
                raise self.gcode.error("Parameter '%s' must have %d values" % (
                    name, count))
            self.delta_analyze_entry[name] = parts
            logging.info("DELTA_ANALYZE %s = %s", name, parts)
        # Perform analysis if requested
        if 'CALIBRATE' in params:
            action = self.gcode.get_str('CALIBRATE', params)
            actions = {'extended': 1}
            if action not in actions:
                raise self.gcode.error("Unknown calibrate action")
            self.do_extended_calibration()

def load_config(config):
    return DeltaCalibrate(config)
