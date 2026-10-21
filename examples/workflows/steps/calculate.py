"""Declare edit_inputs={updated: features}; the service supplies an editable copy."""
arcpy.management.CalculateField(outputs['updated'], parameters['field'],
                                repr(parameters['value']), 'PYTHON3')
