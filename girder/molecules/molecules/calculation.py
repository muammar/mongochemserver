import cherrypy
import tempfile
from jsonpath_rw import parse
from bson.objectid import ObjectId
import json

import openchemistry as oc

from girder.api.describe import Description, autoDescribeRoute
from girder.api.docs import addModel
from girder.api import access
from girder.api.rest import Resource
from girder.api.rest import RestException, getBodyJson, getCurrentUser, \
    loadmodel
from girder.models.model_base import ValidationException
from girder.utility.model_importer import ModelImporter
from girder.models.file import File
from girder.constants import AccessType, SortDir, TokenScope
from molecules.models.calculation import Calculation as CalculationModel
from molecules.utilities.molecules import create_molecule
from molecules.utilities import async_requests

from . import avogadro
from . import openbabel
from .molecule import Molecule

from molecules.models.geometry import Geometry as GeometryModel

class Calculation(Resource):
    output_formats = ['cml', 'xyz', 'inchikey', 'sdf']
    input_formats = ['cml', 'xyz', 'pdb']

    def __init__(self):
        super(Calculation, self).__init__()
        self.resourceName = 'calculations'
        self.route('POST', (), self.create_calc)
        self.route('PUT', (':id', ), self.ingest_calc)
        self.route('DELETE', (':id',), self.delete)
        self.route('GET', (), self.find_calc)
        self.route('GET', ('types',), self.find_calc_types)
        self.route('GET', (':id', 'vibrationalmodes'),
            self.get_calc_vibrational_modes)
        self.route('GET', (':id', 'vibrationalmodes', ':mode'),
            self.get_calc_vibrational_mode)
        self.route('GET', (':id', 'sdf'),
            self.get_calc_sdf)
        self.route('GET', (':id', 'cjson'),
            self.get_calc_cjson)
        self.route('GET', (':id', 'xyz'),
            self.get_calc_xyz)
        self.route('GET', (':id', 'cube', ':mo'),
            self.get_calc_cube)
        self.route('GET', (':id',),
            self.find_id)
        self.route('PUT', (':id', 'properties'),
            self.update_properties)
        self.route('PATCH', (':id', 'notebooks'), self.add_notebooks)

        self._model = ModelImporter.model('calculation', 'molecules')
        self._cube_model = ModelImporter.model('cubecache', 'molecules')

    @access.public
    def get_calc_vibrational_modes(self, id, params):

        # TODO: remove 'cjson' once girder issue #2883 is resolved
        fields = ['cjson', 'cjson.vibrations.modes', 'cjson.vibrations.intensities',
                 'cjson.vibrations.frequencies', 'access']

        calc = self._model.load(id, fields=fields, user=getCurrentUser(),
                                 level=AccessType.READ)

        del calc['access']

        if 'cjson' in calc and 'vibrations' in calc['cjson']:
            return calc['cjson']['vibrations']
        else:
            return {'modes': [], 'intensities': [], 'frequencies': []}

    get_calc_vibrational_modes.description = (
        Description('Get the vibrational modes associated with a calculation')
        .param(
            'id',
            'The id of the calculation to get the modes from.',
            dataType='string', required=True, paramType='path'))

    @access.public
    def get_calc_vibrational_mode(self, id, mode, params):

        try:
            mode = int(mode)
        except ValueError:
            raise ValidationException('mode number be an integer', 'mode')

        # TODO: remove 'cjson' once girder issue #2883 is resolved
        fields = ['cjson', 'cjson.vibrations.modes', 'access']
        calc = self._model.load(id, fields=fields, user=getCurrentUser(),
                                 level=AccessType.READ)

        vibrational_modes = calc['cjson']['vibrations']
        #frames = vibrational_modes.get('modeFrames')
        modes = vibrational_modes.get('modes', [])

        index = modes.index(mode)
        if index < 0:
            raise RestException('No such vibrational mode', 400)

        # Now select the modeFrames directly this seems to be more efficient
        # than iterating in Python
        query = {
            '_id': calc['_id']
        }

        projection = {
            'cjson.vibrations.frequencies': {
                '$slice': [index, 1]
            },
            'cjson.vibrations.intensities': {
                '$slice': [index, 1]
            },
            'cjson.vibrations.eigenVectors': {
                '$slice': [index, 1]
            },
            'cjson.vibrations.modes': {
                '$slice': [index, 1]
            }
        }

        mode = self._model.findOne(query, fields=projection)

        return mode['cjson']['vibrations']

    get_calc_vibrational_mode.description = (
        Description('Get a vibrational mode associated with a calculation')
        .param(
            'id',
            'The id of the calculation that the mode is associated with.',
            dataType='string', required=True, paramType='path')
        .param(
            'mode',
            'The index of the vibrational model to get.',
            dataType='string', required=True, paramType='path'))

    @access.public
    @loadmodel(model='calculation', plugin='molecules', level=AccessType.READ)
    def get_calc_sdf(self, calculation, params):

        def stream():
            cherrypy.response.headers['Content-Type'] = 'chemical/x-mdl-sdfile'
            yield calculation['sdf']

        return stream

    get_calc_sdf.description = (
        Description('Get the molecular structure of a give calculation in SDF format')
        .param(
            'id',
            'The id of the calculation to return the structure for.',
            dataType='string', required=True, paramType='path'))

    @access.public
    @loadmodel(model='calculation', plugin='molecules', level=AccessType.READ)
    def get_calc_cjson(self, calculation, params):
        return calculation['cjson']

    get_calc_cjson.description = (
        Description('Get the molecular structure of a give calculation in CJSON format')
        .param(
            'id',
            'The id of the calculation to return the structure for.',
            dataType='string', required=True, paramType='path'))

    @access.public
    @loadmodel(model='calculation', plugin='molecules', level=AccessType.READ)
    def get_calc_xyz(self, calculation, params):
        data = json.dumps(calculation['cjson'])
        data = avogadro.convert_str(data, 'cjson', 'xyz')

        def stream():
            cherrypy.response.headers['Content-Type'] = Molecule.mime_types['xyz']
            yield data

        return stream

    get_calc_xyz.description = (
        Description('Get the molecular structure of a give calculation in XYZ format')
        .param(
            'id',
            'The id of the calculation to return the structure for.',
            dataType='string', required=True, paramType='path'))

    @access.public
    def get_calc_cube(self, id, mo, params):
        orig_mo = mo
        try:
            mo = int(mo)
        except ValueError:
            # Check for homo lumo
            mo = mo.lower()
            if mo in ['homo', 'lumo']:
                cal = self._model.load(id, force=True)
                # Electron count might be saved in several places...
                path_expressions = [
                    'cjson.orbitals.electronCount',
                    'cjson.basisSet.electronCount',
                    'properties.electronCount'
                ]
                matches = []
                for expr in path_expressions:
                    matches.extend(parse(expr).find(cal))
                if len(matches) > 0:
                    electron_count = matches[0].value
                else:
                    raise RestException('Unable to access electronCount', 400)

                # The index of the first orbital is 0, so homo needs to be
                # electron_count // 2 - 1
                if mo == 'homo':
                    mo = int(electron_count / 2) - 1
                elif mo == 'lumo':
                    mo = int(electron_count / 2)
            else:
                raise ValidationException('mo number be an integer or \'homo\'/\'lumo\'', 'mode')

        cached = self._cube_model.find_mo(id, mo)

        # If we have a cached cube file use that.
        if cached:
            return cached['cjson']

        fields = ['cjson', 'access', 'fileId']

        # Ignoring access control on file/data for now, all public.
        calc = self._model.load(id, fields=fields, force=True)

        # This is where the cube gets calculated, should be cached in future.
        if ('async' in params) and (params['async']):
            async_requests.schedule_orbital_gen(
                calc['cjson'], mo, id, orig_mo, self.getCurrentUser())
            calc['cjson']['cube'] = {
                'dimensions': [0, 0, 0],
                'scalars': []
            }
            return calc['cjson']
        else:
            cjson = avogadro.calculate_mo(calc['cjson'], mo)

            # Remove the vibrational mode data from the cube - big, not needed here.
            if 'vibrations' in cjson:
                del cjson['vibrations']

            # Cache this cube for the next time, they can take a while to generate.
            self._cube_model.create(id, mo, cjson)

            return cjson

    get_calc_cube.description = (
        Description('Get the cube for the supplied MO of the calculation in CJSON format')
        .param(
            'id',
            'The id of the calculation to return the structure for.',
            dataType='string', required=True, paramType='path')
        .param(
            'mo',
            'The molecular orbital to get the cube for.',
            dataType='string', required=True, paramType='path'))

    @access.user(scope=TokenScope.DATA_WRITE)
    def create_calc(self, params):
        body = getBodyJson()
        if 'cjson' not in body and ('fileId' not in body or 'format' not in body):
            raise RestException('Either cjson or fileId is required.')

        user = getCurrentUser()

        cjson = body.get('cjson')
        props = body.get('properties', {})
        molecule_id = body.get('moleculeId', None)
        geometry_id = body.get('geometryId', None)
        public = body.get('public', True)
        notebooks = body.get('notebooks', [])
        image = body.get('image')
        input_parameters = body.get('input', {}).get('parameters')
        if input_parameters is None:
            input_parameters = body.get('inputParameters', {})
        file_id = None
        file_format = body.get('format', 'cjson')

        if 'fileId' in body:
            file = File().load(body['fileId'], user=getCurrentUser())
            file_id = file['_id']
            cjson = self._file_to_cjson(file, file_format)

        if molecule_id is None:
            mol = create_molecule(json.dumps(cjson), 'cjson', user, public, parameters=input_parameters)
            molecule_id = mol['_id']

        calc = CalculationModel().create_cjson(user, cjson, props, molecule_id,
                                               geometry_id=geometry_id,
                                               image=image,
                                               input_parameters=input_parameters,
                                               file_id=file_id,
                                               notebooks=notebooks, public=public)

        cherrypy.response.status = 201
        cherrypy.response.headers['Location'] \
            = '/calculations/%s' % (str(calc['_id']))

        return CalculationModel().filter(calc, user)

    # Try and reuse schema for documentation, this only partially works!
    calc_schema = CalculationModel.schema.copy()
    calc_schema['id'] = 'CalculationData'
    addModel('Calculation', 'CalculationData', calc_schema)

    create_calc.description = (
        Description('Get the molecular structure of a give calculation in SDF format')
        .param(
            'body',
            'The calculation data', dataType='CalculationData', required=True,
            paramType='body'))

    def _file_to_cjson(self, file, file_format):
        readers = {
            'cjson': oc.CjsonReader
        }

        if file_format not in readers:
            raise Exception('Unknown file format %s' % file_format)
        reader = readers[file_format]

        with File().open(file) as f:
            calc_data = f.read().decode()

        # SpooledTemporaryFile doesn't implement next(),
        # workaround in case any reader needs it
        tempfile.SpooledTemporaryFile.__next__ = lambda self: self.__iter__().__next__()

        with tempfile.SpooledTemporaryFile(mode='w+', max_size=10*1024*1024) as tf:
            tf.write(calc_data)
            tf.seek(0)
            cjson = reader(tf).read()

        return cjson

    @access.user(scope=TokenScope.DATA_WRITE)
    @autoDescribeRoute(
        Description('Update pending calculation with results.')
        .modelParam('id', 'The calculation id',
            model=CalculationModel, destName='calculation',
            level=AccessType.WRITE, paramType='path')
        .param('detectBonds',
               'Automatically detect bonds if they are not already present in the ingested molecule',
               required=False,
               dataType='boolean',
               default=False)
        .jsonParam('body', 'The calculation details', required=True, paramType='body')
    )
    def ingest_calc(self, calculation, body, detectBonds=None):
        self.requireParams(['fileId', 'format'], body)

        file = File().load(body['fileId'], user=getCurrentUser())
        cjson = self._file_to_cjson(file, body['format'])

        calc_props = calculation['properties']
        # The calculation is no longer pending
        if 'pending' in calc_props:
            del calc_props['pending']

        # Add bonds if they were not there already
        if detectBonds is None:
            detectBonds = False

        bonds = cjson.get('bonds')
        if bonds is None and detectBonds:
            new_cjson = openbabel.autodetect_bonds(cjson)
            if new_cjson.get('bonds') is not None:
                cjson['bonds'] = new_cjson['bonds']

        calculation['properties'] = calc_props
        calculation['cjson'] = cjson
        calculation['fileId'] = file['_id']

        image = body.get('image')
        if image is not None:
            calculation['image'] = image

        code = body.get('code')
        if code is not None:
            calculation['code'] = code

        scratch_folder_id = body.get('scratchFolderId')
        if scratch_folder_id is not None:
            calculation['scratchFolderId'] = scratch_folder_id

        # If this was a geometry optimization, create a geometry from it
        task = parse('input.parameters.task').find(calculation)
        if task and task[0].value == 'optimize':
            moleculeId = calculation.get('moleculeId')
            provenanceType = 'calculation'
            provenanceId = calculation.get('_id')
            # The cjson will be whitelisted
            geometry = GeometryModel().create(getCurrentUser(), moleculeId,
                                              cjson, provenanceType,
                                              provenanceId)
            calculation['optimizedGeometryId'] = geometry.get('_id')

        return CalculationModel().save(calculation)

    @access.public
    @autoDescribeRoute(
        Description('Search for particular calculation')
        .param('moleculeId', 'The molecule ID linked to this calculation', required=False)
        .param('geometryId', 'The geometry ID linked to this calculation', required=False)
        .param('imageName', 'The name of the Docker image that run this calculation', required=False)
        .param('inputParameters', 'JSON string of the input parameters. May be in percent encoding.', required=False)
        .param('inputGeometryHash', 'The hash of the input geometry.', required=False)
        .param('name', 'The name of the molecule', paramType='query',
                   required=False)
        .param('inchi', 'The InChI of the molecule', paramType='query',
                required=False)
        .param('inchikey', 'The InChI key of the molecule', paramType='query',
                required=False)
        .param('smiles', 'The SMILES of the molecule', paramType='query',
                required=False)
        .param('formula',
                'The formula (using the "Hill Order") to search for',
                paramType='query', required=False)
        .param('creatorId', 'The id of the user that created the calculation',
               required=False)
        .pagingParams(defaultSort='_id', defaultSortDir=SortDir.DESCENDING, defaultLimit=25)
    )
    def find_calc(self, moleculeId=None, geometryId=None, imageName=None,
                  inputParameters=None, inputGeometryHash=None,
                  name=None, inchi=None, inchikey=None, smiles=None,
                  formula=None, creatorId=None, pending=None, limit=None,
                  offset=None, sort=None):
        return CalculationModel().findcal(
            molecule_id=moleculeId, geometry_id=geometryId,
            image_name=imageName, input_parameters=inputParameters,
            input_geometry_hash=inputGeometryHash, name=name, inchi=inchi,
            inchikey=inchikey, smiles=smiles, formula=formula,
            creator_id=creatorId, pending=pending, limit=limit, offset=offset,
            sort=sort, user=getCurrentUser())

    @access.public
    def find_id(self, id, params):
        user = getCurrentUser()
        cal = self._model.load(id, level=AccessType.READ, user=user)
        if not cal:
            raise RestException('Calculation not found.', code=404)

        return cal
    find_id.description = (
        Description('Get the calculation by id')
        .param(
            'id',
            'The id of calculation.',
            dataType='string', required=True, paramType='path'))

    @access.user(scope=TokenScope.DATA_WRITE)
    def delete(self, id, params):
        user = getCurrentUser()
        cal = self._model.load(id, level=AccessType.READ, user=user)
        if not cal:
            raise RestException('Calculation not found.', code=404)

        return self._model.remove(cal, user)
    delete.description = (
        Description('Delete a calculation by id.')
        .param(
            'id',
            'The id of calculatino.',
            dataType='string', required=True, paramType='path'))

    @access.public
    def find_calc_types(self, params):
        fields = ['access', 'properties.calculationTypes']

        query = {}
        if 'moleculeId' in params:
            query['moleculeId'] = ObjectId(params['moleculeId'])

        calcs = self._model.find(query, fields=fields)

        allTypes = []
        for types in calcs:
            calc_types = parse('properties.calculationTypes').find(types)
            if calc_types:
                calc_types = calc_types[0].value
                allTypes.extend(calc_types)

        typeSet = set(allTypes)

        return list(typeSet)

    find_calc_types.description = (
        Description('Get the calculation types available for the molecule')
        .param(
            'moleculeId',
            'The id of the molecule we are finding types for.',
            dataType='string', required=True, paramType='query'))

    @access.user(scope=TokenScope.DATA_WRITE)
    @autoDescribeRoute(
        Description('Update the calculation properties.')
        .notes('Override the exist properties')
        .modelParam('id', 'The ID of the calculation.', model='calculation',
                    plugin='molecules', level=AccessType.ADMIN)
        .param('body', 'The new set of properties', paramType='body')
        .errorResponse('ID was invalid.')
        .errorResponse('Write access was denied for the calculation.', 403)
    )
    def update_properties(self, calculation, params):
        props = getBodyJson()
        calculation['properties'] = props
        calculation = self._model.save(calculation)

        return calculation

    @access.user(scope=TokenScope.DATA_WRITE)
    @autoDescribeRoute(
        Description('Add notebooks ( file ids ) to molecule.')
        .modelParam('id', 'The calculation id',
                    model=CalculationModel, destName='calculation',
                    force=True, paramType='path')
        .jsonParam('notebooks', 'List of notebooks', required=True, paramType='body')
    )
    def add_notebooks(self, calculation, notebooks):
        notebooks = notebooks.get('notebooks')
        if notebooks is not None:
            CalculationModel().add_notebooks(calculation, notebooks)
