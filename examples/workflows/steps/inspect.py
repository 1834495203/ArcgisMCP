"""Draft analysis body; submit with outputs={}."""
count = int(arcpy.management.GetCount(inputs['features'])[0])
print({'count': count, 'fields': [field.name for field in arcpy.ListFields(inputs['features'])]})
if parameters.get('expected_count') is not None:
    step.check('caller expected count', count == parameters['expected_count'],
               {'actual': count, 'expected': parameters['expected_count']})
