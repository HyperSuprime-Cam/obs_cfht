import math

import lsst.daf.base as dafBase
import lsst.pex.config as pexConfig
import lsst.afw.detection as afwDet
import lsst.afw.math as afwMath
import lsst.afw.table as afwTable
import lsst.afw.coord as afwCoord
import lsst.afw.geom as afwGeom
import lsst.meas.algorithms as measAlg
import lsst.pipe.base as pipeBase
import lsst.pipe.tasks.calibrate as ptcalibrate
from lsst.afw.image import fluxFromABMag, fluxErrFromABMagErr

import numpy as np
import matplotlib
import matplotlib.pylab as plt

class CfhtCalibrateTask(ptcalibrate.CalibrateTask) :
	
    def run(self, exposure, defects=None, idFactory=None):
        """!Run the calibration task on an exposure

        \param[in,out]  exposure   Exposure to calibrate; measured PSF will be installed there as well
        \param[in]      defects    List of defects on exposure
        \param[in]      idFactory  afw.table.IdFactory to use for source catalog.
        \return a pipeBase.Struct with fields:
        - exposure: Repaired exposure
        - backgrounds: A list of background models applied in the calibration phase
        - psf: Point spread function
        - sources: Sources used in calibration
        - matches: Astrometric matches
        - matchMeta: Metadata for astrometric matches
        - photocal: Output of photocal subtask

        It is moderately important to provide a decent initial guess for the seeing if you want to
        deal with cosmic rays.  If there's a PSF in the exposure it'll be used; failing that the
        CalibrateConfig.initialPsf is consulted (although the pixel scale will be taken from the
        WCS if available).

        If the exposure contains an lsst.afw.image.Calib object with the exposure time set, MAGZERO
        will be set in the task metadata.
        """
        assert exposure is not None, "No exposure provided"

        if not exposure.hasPsf():
            self.installInitialPsf(exposure)
        if idFactory is None:
            idFactory = afwTable.IdFactory.makeSimple()
        backgrounds = afwMath.BackgroundList()
        keepCRs = True                  # At least until we know the PSF
        self.repair.run(exposure, defects=defects, keepCRs=keepCRs)
        self.display('repair', exposure=exposure)
        if self.config.doBackground:
            with self.timer("background"):
                bg, exposure = measAlg.estimateBackground(exposure, self.config.background, subtract=True)
                backgrounds.append(bg)
            self.display('background', exposure=exposure)

        # Make both tables from the same detRet, since detRet can only be run once
        table1 = afwTable.SourceTable.make(self.schema1, idFactory)
        table1.setMetadata(self.algMetadata)
        detRet = self.detection.makeSourceCatalog(table1, exposure)
        sources1 = detRet.sources


        if detRet.fpSets.background:
            backgrounds.append(detRet.fpSets.background)

        if self.config.doPsf:
            self.initialMeasurement.measure(exposure, sources1)

 # ### Do not compute astrometry before PSF determination. Astrometry will be computed afterwards
 # ###
 #           if self.config.doAstrometry:
 #               astromRet = self.astrometry.run(exposure, sources1)
 #               matches = astromRet.matches
 #           else:
                # If doAstrometry is False, we force the Star Selector to either make them itself
                # or hope it doesn't need them.
 #               matches = None
            matches = None
            psfRet = self.measurePsf.run(exposure, sources1, matches=matches)
            cellSet = psfRet.cellSet
            psf = psfRet.psf
        elif exposure.hasPsf():
            psf = exposure.getPsf()
            cellSet = None
        else:
            psf, cellSet = None, None

        # Wash, rinse, repeat with proper PSF

        if self.config.doPsf:
            self.repair.run(exposure, defects=defects, keepCRs=None)
            self.display('PSF_repair', exposure=exposure)

        if self.config.doBackground:
            # Background estimation ignores (by default) pixels with the
            # DETECTED bit set, so now we re-estimate the background,
            # ignoring sources.  (see BackgroundConfig.ignoredPixelMask)
            with self.timer("background"):
                # Subtract background
                bg, exposure = measAlg.estimateBackground(
                    exposure, self.config.background, subtract=True,
                    statsKeys=('BGMEAN2', 'BGVAR2'))
                self.log.info("Fit and subtracted background")
                backgrounds.append(bg)

            self.display('PSF_background', exposure=exposure)

        if self.config.doAstrometry or self.config.doPhotoCal:
            # make a second table with which to do the second measurement
            # the schemaMapper will copy the footprints and ids, which is all we need.
            table2 = afwTable.SourceTable.make(self.schema, idFactory)
            table2.setMetadata(self.algMetadata)
            sources = afwTable.SourceCatalog(table2)
            # transfer to a second table
            sources.extend(sources1, self.schemaMapper)
            self.measurement.run(exposure, sources)
        else:
            sources = sources1

        if self.config.doAstrometry:
            astromRet = self.astrometry.run(exposure, sources)
            matches = astromRet.matches
            matchMeta = astromRet.matchMeta
        else:
            matches, matchMeta = None, None

        if self.config.doPhotoCal:
            matchPhot = self.doPhotocalMatch(sources).match
            assert(matchPhot is not None)
            try:
                photocalRet = self.photocal.run(exposure, matchPhot)
            except Exception, e:
                raise
                self.log.warn("Failed to determine photometric zero-point: %s" % e)
                photocalRet = None
                self.metadata.set('MAGZERO', float("NaN"))

            if photocalRet:
                self.log.info("Photometric zero-point: %f" % photocalRet.calib.getMagnitude(1.0))
                exposure.getCalib().setFluxMag0(photocalRet.calib.getFluxMag0())
                metadata = exposure.getMetadata()
                # convert to (mag/sec/adu) for metadata
                try:
                    magZero = photocalRet.zp - 2.5 * math.log10(exposure.getCalib().getExptime() )
                    metadata.set('MAGZERO', magZero)
                except:
                    self.log.warn("Could not set normalized MAGZERO in header: no exposure time")
                metadata.set('MAGZERO_RMS', photocalRet.sigma)
                metadata.set('MAGZERO_NOBJ', photocalRet.ngood)
                metadata.set('COLORTERM1', 0.0)
                metadata.set('COLORTERM2', 0.0)
                metadata.set('COLORTERM3', 0.0)
        else:
            photocalRet = None
        self.display('calibrate', exposure=exposure, sources=sources, matches=matches)
        
        return pipeBase.Struct(
            exposure = exposure,
            backgrounds = backgrounds,
            psf = psf,
            sources = sources,
            matches = matches,
            matchMeta = matchMeta,
            photocal = photocalRet,
        )

    def doPhotocalMatch(self, sources) :
        # Read external catalog for photometry calibration and load it info an afw table
        # Then match it with the sources from the current exposure and return the matched catalog
        
        file_cat = "../Catalogs/CFHTLS_D3_stars_mag_r_20.cat"
        with open(file_cat, 'r') as f:
            lines = f.readlines()
            
        schema  = afwTable.SourceTable.makeMinimalSchema()
        filters='ugriz'

        for f in filters :
            schema.addField(f + "_flux", type="F", doc="Flux" + f)
            schema.addField(f + "_fluxSigma", type="F", doc="Error on flux" + f)
        schema.addField("photometric", type="Flag", doc="Photometric quality flag")
        
        catalog = afwTable.SimpleCatalog(schema)
        for i in range(2,len(lines)) :
            l = lines[i].split()
            record = catalog.addNew()
            coord = afwCoord.Coord(afwGeom.Point2D(np.float64(l[9]),np.float64(l[10])))
            record.set("coord",coord)
            for cnts,f in enumerate(filters) :
                # Need to convert magnitude to flux in order to be compatible with the rest
                record.set(f + "_flux", fluxFromABMag(float(l[11+2*cnts])))
                record.set(f + "_fluxSigma", fluxErrFromABMagErr(float(l[12+2*cnts]),float(l[11+2*cnts])))
 #           if int(l[21]) == 0 and int(l[22]) == 1 :
            record.set("photometric", True)
#            else :
#                record.set("photometric", False)

        # Make a deep copy in order to have everything contiguous in memory
        ref = catalog.copy(deep=True)
        self.log.info("Photometric reference catalog has been loaded with %d CFHT reference objects"%(len(lines)-2))
        
        # Match sources and photmetric reference catalog
        match = afwTable.matchRaDec(ref, sources, 1.0 * afwGeom.arcseconds)
        self.log.info("Found %d matches in photometric reference catalog"%(len(match)))
        
        return pipeBase.Struct(
            match = match,
        )
