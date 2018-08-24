""" Module that uses CMAC 2.0 to remove and correct second trip returns,
correct velocity and more. A new radar object is then created with all CMAC
2.0 products. """

import copy
import json
import sys

import netCDF4
import pyart
import numpy as np

from . import cmac_processing


def cmac(radar, sonde, config,
         meta_append=None, verbose=True):
    """
    Corrected Moments in Antenna Coordinates

    Parameters
    ----------
    radar : Radar
        Radar object to use in the CMAC calculation.
    sonde : Object
        Object containing all the sonde data.
    config : dict
        A dictionary containing different values specific to a radar and
        a sounding needed in the cmac processing.

    Other Parameters
    ----------------
    meta_append : dict, json and None
        Value key pairs to attend to global attributes. If None,
        a default metadata will be created. The metadata can also
        be created by providing a dictionary or a json file.
    verbose: bool
        If True this will display more statistics.

    Returns
    -------
    radar : Radar
        Radar object with new CMAC added fields.

    """
    # Obtaining variables needed for fuzzy logic.
    radar.altitude['data'][0] = config['site_alt']

    radar_start_date = netCDF4.num2date(
        radar.time['data'][0], radar.time['units'])
    print('##', str(radar_start_date))

    sonde_temp = config['sonde']['temperature']
    sonde_alt = config['sonde']['height']

    z_dict, temp_dict = pyart.retrieve.map_profile_to_gates(
        sonde.variables[sonde_temp][:], sonde.variables[sonde_alt][:], radar)
    texture = cmac_processing.get_texture(radar)

    snr = pyart.retrieve.calculate_snr_from_reflectivity(radar)
    if not verbose:
        print('## Adding radar fields...')

    if verbose:
        print('##')
        print('## These radar fields are being added:')

    radar.add_field('sounding_temperature', temp_dict, replace_existing=True)
    radar.add_field('height', z_dict, replace_existing=True)
    radar.add_field('signal_to_noise_ratio', snr, replace_existing=True)
    radar.add_field('velocity_texture', texture, replace_existing=True)
    if verbose:
        print('##    sounding_temperature')
        print('##    height')
        print('##    signal_to_noise_ratio')
        print('##    velocity_texture')

    # Performing fuzzy logic to obtain the gate ids.
    my_fuzz, _ = cmac_processing.do_my_fuzz(radar, tex_start=2.4,
                                            tex_end=2.7, verbose=verbose)
    radar.add_field('gate_id', my_fuzz,
                    replace_existing=True)

    # Adding fifth gate id, clutter.
    clutter_data = radar.fields['xsapr_clutter']['data']
    radar.fields['gate_id']['data'][clutter_data == 1] = 5
    notes = radar.fields['gate_id']['notes']
    radar.fields['gate_id']['notes'] = notes + ',5:clutter'
    radar.fields['gate_id']['valid_max'] = 5
    cat_dict = {}
    for pair_str in radar.fields['gate_id']['notes'].split(','):
        cat_dict.update(
            {pair_str.split(':')[1]:int(pair_str.split(':')[0])})

    if verbose:
        print('##    gate_id')

    # Corrected velocity using pyart's region dealiaser.
    cmac_gates = pyart.correct.GateFilter(radar)
    cmac_gates.exclude_all()
    cmac_gates.include_equal('gate_id', cat_dict['rain'])
    cmac_gates.include_equal('gate_id', cat_dict['melting'])
    cmac_gates.include_equal('gate_id', cat_dict['snow'])
    corr_vel = pyart.correct.dealias_region_based(
        radar, vel_field='velocity', keep_original=False,
        gatefilter=cmac_gates, centered=True)

    radar.add_field('corrected_velocity', corr_vel, replace_existing=True)
    if verbose:
        print('##    corrected_velocity')

    fzl = cmac_processing.get_melt(radar)

    # Calculating differential phase fields.
    phidp, kdp = pyart.correct.phase_proc_lp(radar, 0.0, debug=True,
                                             nowrap=50, fzl=fzl)
    phidp_filt, kdp_filt = cmac_processing.fix_phase_fields(
        copy.deepcopy(kdp), copy.deepcopy(phidp), radar.range['data'],
        cmac_gates)

    radar.add_field('corrected_differential_phase', phidp)
    radar.add_field('filtered_corrected_differential_phase', phidp_filt)
    radar.add_field('corrected_specific_diff_phase', kdp)
    radar.add_field('filtered_corrected_specific_diff_phase', kdp_filt)
    if verbose:
        print('##    corrected_specific_diff_phase')
        print('##    filtered_corrected_specific_diff_phase')
        print('##    corrected_differential_phase')
        print('##    filtered_corrected_differential_phase')

    # Calculating attenuation by using pyart.
    attenuation_a_coef = config['attenuation_a_coef']
    spec_at, cor_z_atten = pyart.correct.calculate_attenuation(
        radar, 0, refl_field='reflectivity',
        ncp_field='normalized_coherent_power',
        rhv_field='cross_correlation_ratio',
        phidp_field='filtered_corrected_differential_phase',
        a_coef=attenuation_a_coef)

    cat_dict = {}
    for pair_str in radar.fields['gate_id']['notes'].split(','):
        if verbose:
            print(pair_str)
        cat_dict.update({pair_str.split(':')[1]: int(pair_str.split(':')[0])})

    rain_gates = pyart.correct.GateFilter(radar)
    rain_gates.exclude_all()
    rain_gates.include_equal('gate_id', cat_dict['rain'])
    spec_at['data'][rain_gates.gate_excluded] = 0.0

    radar.add_field('specific_attenuation', spec_at)
    radar.add_field('attenuation_corrected_reflectivity', cor_z_atten)
    if verbose:
        print('##    specific_attenuation')
        print('##    attenuation_corrected_reflectivity')

    # Calculating rain rate.
    R = 51.3 * (radar.fields['specific_attenuation']['data']) ** 0.81
    rainrate = copy.deepcopy(radar.fields['specific_attenuation'])
    rainrate['data'] = R
    rainrate['valid_min'] = 0.0
    rainrate['valid_max'] = 400.0
    rainrate['standard_name'] = 'rainfall_rate'
    rainrate['long_name'] = 'rainfall_rate'
    rainrate['least_significant_digit'] = 1
    rainrate['units'] = 'mm/hr'
    radar.fields.update({'rain_rate_A': rainrate})

    # This needs to be updated to a gatefilter.
    mask = radar.fields['reflectivity']['data'].mask

    radar.fields['rain_rate_A']['data'][np.where(mask)] = 0.0
    radar.fields['rain_rate_A'].update({
        'comment': ('Rain rate calculated from specific_attenuation,',
                    ' R=51.3*specific_attenuation**0.81, note R=0.0 where',
                    ' norm coherent power < 0.4 or rhohv < 0.8')})

    if verbose:
        print('## Rainfall rate as a function of A ##')

    print('##')
    print('## All CMAC fields have been added to the radar object.')
    print('##')

    # Adding the metadata to the cmac radar object.
    print('## Appending metadata')
    command_line = ''
    for item in sys.argv:
            command_line = command_line + ' ' + item
    if meta_append is None:
        meta = {
            'site_id': 'sgp',
            'data_level': 'c1',
            'comment': (
                'This is highly experimental and initial data. There are many',
                'known and unknown issues. Please do not use before',
                'contacting the Translator responsible scollis@anl.gov'),
            'attributions': (
                'This data is collected by the ARM Climate Research facility.',
                'Radar system is operated by the radar engineering team',
                'radar@arm.gov and the data is processed by the precipitation',
                'radar products team. LP code courtesy of Scott Giangrande',
                'BNL.'),
            'version': '2.0 lite',
            'vap_name': 'cmac',
            'known_issues': (
                'False phidp jumps in insect regions. Still uses old',
                'Giangrande code.'),
            'developers': 'Robert Jackson, ANL. Zachary Sherman, ANL.',
            'translator': 'Scott Collis, ANL.',
            'mentors': ('Nitin Bharadwaj, PNNL. Bradley Isom, PNNL.',
                        'Joseph Hardin, PNNL. Iosif Lindenmaier, PNNL.')}
    else:
        if meta_append.lower().endswith('.json'):
            with open(meta_append, 'r') as infile:
                meta = json.load(infile)
        elif meta_append == 'config':
            meta = config['metadata']
        else:
            raise RuntimeError('Must provide the file name of the json file',
                               'or say config to use the meta data from',
                               'config.py')

    radar.metadata.clear()
    radar.metadata.update(meta)
    radar.metadata['command_line'] = command_line
    return radar