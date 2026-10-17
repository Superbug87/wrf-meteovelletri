! Collaudo: rilegge un file OBS_DOMAINx01 con gli stessi formati di WRF (share/wrf_fddaobs_in.F, in4dob).
! Uso: gfortran leggi_obs.f90 -o leggi_obs && ./leggi_obs OBS_DOMAIN301
program lettore
  implicit none
  character(len=14) :: date_char
  real :: latitude, longitude, elevation
  character(len=40) :: id, namef
  character(len=16) :: platform, source
  logical :: is_sound, bogus
  integer :: meas_count, n, ios
  real :: slp_data, slp_qc, ref_pres_data, ref_pres_qc, height_data, height_qc
  real :: temperature_data, temperature_qc, u_met_data, u_met_qc, v_met_data, v_met_qc
  real :: rh_data, rh_qc, psfc_data, psfc_qc, precip_data, precip_qc
  character(len=200) :: nome
  call get_command_argument(1, nome)
  open(10, file=trim(nome), form='FORMATTED', status='OLD')
  n = 0
  do
    read(10,101,iostat=ios) date_char
    if (ios /= 0) exit
 101 format(1x,a14)
    read(10,102,err=9) latitude, longitude
 102 format(2x,2(f9.4,1x))
    read(10,1021,err=9) id, namef
 1021 format(2x,2(a40,3x))
    read(10,103,err=9) platform, source, elevation, is_sound, bogus, meas_count
 103 format( 2x,2(a16,2x),f8.0,2x,2(l4,2x),i5)
    read(10,105,err=9) slp_data,slp_qc, ref_pres_data,ref_pres_qc, height_data,height_qc, &
                 temperature_data,temperature_qc, u_met_data,u_met_qc, v_met_data,v_met_qc, &
                 rh_data,rh_qc, psfc_data,psfc_qc, precip_data,precip_qc
 105 format( 1x,9(f11.3,1x,f11.3,1x))
    n = n + 1
  end do
  print '(a,i6)', 'osservazioni lette: ', n
  if (n == 0) stop 1
  stop
9 print '(a,i6)', 'ERRORE di lettura all''osservazione ', n + 1
  stop 1
end program
