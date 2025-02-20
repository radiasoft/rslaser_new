from rslaser.utils.validator import ValidatorBase
import copy
import numpy as np
from pykern.pkcollections import PKDict
from rsmath import lct as rslct
import srwlib
from srwlib import srwl
import scipy.constants as const
import rslaser.utils.srwl_uti_data as srwutil
from scipy.interpolate import RectBivariateSpline


class ElementException(Exception):
    pass


class Element(ValidatorBase):
    def propagate(self, laser_pulse, prop_type="default"):

        if prop_type != "default":
            raise ElementException(
                f'Non default prop_type "{prop_type}" passed to propagation'
            )

        if self.prop_type == "srw":
            if not hasattr(self, "_srwc"):
                raise ElementException(f"_srwc field is expected to be set on {self}")
            for w in laser_pulse.slice:
                srwl.PropagElecField(w.wfr, self._srwc)
        elif self.prop_type == "lct":
            laser_pulse = _prop_abcd_lct(laser_pulse, self.abcd_matrix, self.l_scale)
        elif self.prop_type == "beamsplitter":
            laser_pulse = _split_beam(laser_pulse, self.transmitted_fraction)

        laser_pulse.update_photon_positions()
        return laser_pulse


def _interp_to_odd(x_old, y_old, mesh_old):

    nx, ny = len(x_old), len(y_old)
    if nx % 2 == 0:
        x_new = np.linspace(np.min(x_old), np.max(x_old), nx + 1)
    else:
        x_new = np.copy(x_old)
    if ny % 2 == 0:
        y_new = np.linspace(np.min(y_old), np.max(y_old), ny + 1)
    else:
        y_new = np.copy(y_old)

    if nx % 2 == 0 or ny % 2 == 0:
        mesh_new = {}
        for mesh in mesh_old:
            pre_interp = mesh_old["{}".format(mesh)]
            rect_biv_spline = RectBivariateSpline(x_old, y_old, pre_interp)
            post_interp = rect_biv_spline(x_new, y_new)
            mesh_new["{}".format(mesh)] = post_interp
    else:
        mesh_new = copy.deepcopy(mesh_old)

    return x_new, y_new, mesh_new


def _prop_abcd_lct(laser_pulse, abcd_mat, l_scale):
    nslices_pulse = laser_pulse.nslice

    def _wfr_prop_abcd_lct(abcd_mat_cryst, l_scale, photon_e_ev, wfr0):
        re0_2d_ex, im0_2d_ex, re0_2d_ey, im0_2d_ey = srwutil.extract_2d_fields(wfr0)

        xvals_slice = np.linspace(wfr0.mesh.xStart, wfr0.mesh.xFin, wfr0.mesh.nx)
        yvals_slice = np.linspace(wfr0.mesh.yStart, wfr0.mesh.yFin, wfr0.mesh.ny)

        mesh_old = {
            "re0_2d_ex": re0_2d_ex,
            "im0_2d_ex": im0_2d_ex,
            "re0_2d_ey": re0_2d_ey,
            "im0_2d_ey": im0_2d_ey,
        }
        xvals_slice, yvals_slice, mesh_new = _interp_to_odd(
            xvals_slice, yvals_slice, mesh_old
        )

        Etot0_2d_x = mesh_new["re0_2d_ex"] + 1j * mesh_new["im0_2d_ex"]
        Etot0_2d_y = mesh_new["re0_2d_ey"] + 1j * mesh_new["im0_2d_ey"]

        dX = xvals_slice[1] - xvals_slice[0]  # horizontal spacing [m]
        dX_scale = dX / l_scale
        dY = yvals_slice[1] - yvals_slice[0]  # vertical spacing [m]
        dY_scale = dY / l_scale

        # define horizontal and vertical input signals
        in_signal_2d_x = (dX_scale, dY_scale, Etot0_2d_x)
        in_signal_2d_y = (dX_scale, dY_scale, Etot0_2d_y)

        # calculate 2D LCTs
        dX_out, dY_out, out_signal_2d_x = rslct.apply_lct_2d_sep(
            abcd_mat_cryst, abcd_mat_cryst, in_signal_2d_x
        )
        dX_out, dY_out, out_signal_2d_y = rslct.apply_lct_2d_sep(
            abcd_mat_cryst, abcd_mat_cryst, in_signal_2d_y
        )

        re_out_signal_2d_x = np.real(out_signal_2d_x)
        x_total = (np.shape(re_out_signal_2d_x)[0] - 1) * dX_out
        y_total = (np.shape(re_out_signal_2d_x)[1] - 1) * dY_out
        xold = np.linspace(
            -x_total / 2.0, x_total / 2.0, np.shape(re_out_signal_2d_x)[0]
        )
        yold = np.linspace(
            -y_total / 2.0, y_total / 2.0, np.shape(re_out_signal_2d_x)[1]
        )

        mesh_old_2 = {
            "re_out_signal_2d_x": np.real(out_signal_2d_x),
            "im_out_signal_2d_x": np.imag(out_signal_2d_x),
            "re_out_signal_2d_y": np.real(out_signal_2d_y),
            "im_out_signal_2d_y": np.imag(out_signal_2d_y),
        }
        xnew, ynew, mesh_new = _interp_to_odd(xold, yold, mesh_old_2)

        if (
            np.shape(re_out_signal_2d_x)[0] % 2 == 0
            or np.shape(re_out_signal_2d_x)[1] % 2 == 0
        ):
            dX_out = np.mean(np.diff(xnew))
            dY_out = np.mean(np.diff(ynew))

        out_signal_2d_x = (
            mesh_new["re_out_signal_2d_x"] + 1j * mesh_new["im_out_signal_2d_x"]
        )
        out_signal_2d_y = (
            mesh_new["re_out_signal_2d_y"] + 1j * mesh_new["im_out_signal_2d_y"]
        )

        # extract propagated complex field and calculate corresponding x and y mesh arrays
        # we assume same mesh for both components of E_field
        hx = dX_out * l_scale
        hy = dY_out * l_scale
        ny, nx = np.shape(out_signal_2d_x)
        local_xv = rslct.lct_abscissae(nx, hx)
        local_yv = rslct.lct_abscissae(ny, hy)

        # remake the wavefront
        wfr_new = srwutil.make_wavefront(
            np.real(out_signal_2d_x),
            np.imag(out_signal_2d_x),
            np.real(out_signal_2d_y),
            np.imag(out_signal_2d_y),
            photon_e_ev,
            np.linspace(np.min(local_xv), np.max(local_xv), nx),
            np.linspace(np.min(local_xv), np.max(local_xv), ny),
        )

        return wfr_new

    hc_ev_um = 1.23984198  # hc [eV*um]
    for j in np.arange(nslices_pulse):
        thisSlice = laser_pulse.slice[j]

        phLambda = hc_ev_um / thisSlice.photon_e_ev * 1e-6
        abcd_mat_cryst = np.array(
            [
                [abcd_mat.A, abcd_mat.B * phLambda / (l_scale**2)],
                [abcd_mat.C / phLambda * (l_scale**2), abcd_mat.D],
            ]
        )

        wfr0 = thisSlice.wfr
        thisSlice.wfr = _wfr_prop_abcd_lct(
            abcd_mat_cryst, l_scale, thisSlice.photon_e_ev, wfr0
        )

        for k in np.arange(thisSlice.bw_nslice):
            thisSubSlice = thisSlice.bandwidth_slice[k]

            phLambda = hc_ev_um / thisSubSlice.photon_e_ev * 1e-6
            abcd_mat_cryst = np.array(
                [
                    [abcd_mat.A, abcd_mat.B * phLambda / (l_scale**2)],
                    [abcd_mat.C / phLambda * (l_scale**2), abcd_mat.D],
                ]
            )

            wfr0 = thisSubSlice.wfr
            thisSubSlice.wfr = _wfr_prop_abcd_lct(
                abcd_mat_cryst, l_scale, thisSubSlice.photon_e_ev, wfr0
            )

    laser_pulse.resize_laser_mesh()
    return laser_pulse


def _split_beam(laser_pulse, transmitted_fraction):
    # Assume no loss to reflective layer absorption

    def _wfr_split_beam(photon_e_ev, transmitted_fraction, wfr0):

        intensity_2d = srwutil.calc_int_from_elec(wfr0)
        phase_1d = srwlib.array("d", [0] * wfr0.mesh.nx * wfr0.mesh.ny)
        srwl.CalcIntFromElecField(phase_1d, wfr0, 0, 4, 3, wfr0.mesh.eStart, 0, 0)
        phase_2d = (
            np.array(phase_1d)
            .reshape((wfr0.mesh.nx, wfr0.mesh.ny), order="C")
            .astype(np.float64)
        )

        split_intensity = intensity_2d * transmitted_fraction

        split_e_norm = np.sqrt(2.0 * split_intensity / (const.c * const.epsilon_0))
        new_re0_ex = np.multiply(split_e_norm, np.cos(phase_2d))
        new_im0_ex = np.multiply(split_e_norm, np.sin(phase_2d))
        new_re0_ey = np.zeros(np.shape(new_re0_ex))
        new_im0_ey = np.zeros(np.shape(new_im0_ex))

        # remake the wavefront
        wfr_new = srwutil.make_wavefront(
            new_re0_ex,
            new_im0_ex,
            new_re0_ey,
            new_im0_ey,
            photon_e_ev,
            np.linspace(wfr0.mesh.xStart, wfr0.mesh.xFin, wfr0.mesh.nx),
            np.linspace(wfr0.mesh.yStart, wfr0.mesh.yFin, wfr0.mesh.ny),
        )

        return wfr_new

    for j in np.arange(laser_pulse.nslice):
        thisSlice = laser_pulse.slice[j]
        thisSlice.n_photons_2d.mesh *= transmitted_fraction
        thisSlice.wfr = _wfr_split_beam(
            thisSlice.photon_e_ev, transmitted_fraction, thisSlice.wfr
        )

        for k in np.arange(thisSlice.bw_nslice):
            thisSubSlice = thisSlice.bandwidth_slice[k]
            thisSubSlice.n_photons_2d.mesh *= transmitted_fraction
            thisSubSlice.wfr = _wfr_split_beam(
                thisSubSlice.photon_e_ev, transmitted_fraction, thisSubSlice.wfr
            )

    return laser_pulse
